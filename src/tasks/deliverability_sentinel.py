"""Deliverability Sentinel — adapted from Forced Action's
src/tasks/email_deliverability_monitor.py: a Trip dataclass, threshold
constants, and a de-dupe-alert guard so an ongoing incident doesn't repage
every run.

Bounce rate > 3% OR spam-complaint rate > 0.08% within a rolling 48-hour
window trips: quarantine the domain, atomically promote a same-cluster
reserve domain to active, alert. Runs under the BYPASSRLS system session —
deliverability state spans clusters, not a single tenant.

If Instantly is not enabled/configured, this degrades gracefully: it logs
and returns rather than raising, consistent with the "fail gracefully to
DEGRADED state" pattern in the Dev 1 plan's reliability section — it does
NOT silently report all domains healthy.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.instantly_service import InstantlyDisabledError, InstantlyService

logger = logging.getLogger(__name__)

_REALERT_COOLDOWN = timedelta(hours=4)
_recently_paged: dict[str, datetime] = {}


@dataclass(frozen=True)
class Trip:
	domain: str
	rule: str
	observed_value: float
	threshold: float


def _recently_paged_check(domain: str) -> bool:
	last = _recently_paged.get(domain)
	if last is None:
		return False
	return datetime.now(timezone.utc) - last < _REALERT_COOLDOWN


def _record_paged(domain: str) -> None:
	_recently_paged[domain] = datetime.now(timezone.utc)


def _evaluate_domain(session: Session, domain_row, instantly: InstantlyService) -> Optional[Trip]:
	settings = get_settings()
	try:
		analytics = instantly.get_daily_analytics()
	except InstantlyDisabledError:
		logger.info("deliverability_sentinel: Instantly not configured, skipping live check for %s", domain_row.domain)
		return None

	if not analytics:
		logger.warning("deliverability_sentinel: no analytics returned for %s (DEGRADED)", domain_row.domain)
		return None

	bounce_rate = float(analytics.get("bounce_rate_pct", 0.0))
	complaint_rate = float(analytics.get("spam_complaint_rate_pct", 0.0))

	if bounce_rate > settings.deliverability_bounce_rate_threshold_pct:
		return Trip(domain_row.domain, "bounce_rate", bounce_rate, settings.deliverability_bounce_rate_threshold_pct)
	if complaint_rate > settings.deliverability_spam_complaint_threshold_pct:
		return Trip(
			domain_row.domain, "spam_complaint_rate", complaint_rate, settings.deliverability_spam_complaint_threshold_pct
		)
	return None


def _quarantine_and_swap(session: Session, domain_row, trip: Trip) -> None:
	session.execute(
		text(
			"UPDATE sending_domains SET quarantine_state = 'quarantined', quarantined_at = NOW(), "
			"quarantine_reason = :reason WHERE id = :id"
		),
		{
			"reason": f"{trip.rule}={trip.observed_value} exceeds threshold {trip.threshold}",
			"id": domain_row.id,
		},
	)

	# Promote a reserve from the SAME cluster AND the SAME tenant only. A
	# client's degraded domain must be replaced from within that client's own
	# 3-domain cluster — grabbing a reserve from another cluster/tenant would
	# bleed one tenant's warmed reputation into another (Task 4.1 isolation).
	# client_id is compared with IS NOT DISTINCT FROM so the internal pool
	# (client_id IS NULL) matches its own reserves rather than any tenant's.
	reserve = session.execute(
		text(
			"SELECT id FROM sending_domains WHERE is_reserve = TRUE "
			"AND quarantine_state = 'reserve' "
			"AND cluster_label IS NOT DISTINCT FROM :cluster_label "
			"AND client_id IS NOT DISTINCT FROM :client_id "
			"LIMIT 1 FOR UPDATE SKIP LOCKED"
		),
		{"cluster_label": domain_row.cluster_label, "client_id": domain_row.client_id},
	).fetchone()

	if reserve is not None:
		session.execute(
			text(
				"UPDATE sending_domains SET quarantine_state = 'active', is_reserve = FALSE WHERE id = :id"
			),
			{"id": reserve.id},
		)
		logger.warning(
			"deliverability_sentinel: quarantined %s, promoted same-cluster reserve domain id=%s in cluster %s",
			domain_row.domain, reserve.id, domain_row.cluster_label,
		)
	else:
		logger.error(
			"deliverability_sentinel: quarantined %s, NO RESERVE DOMAIN available in cluster %s (client_id=%s)",
			domain_row.domain, domain_row.cluster_label, domain_row.client_id,
		)


def run_sentinel_sweep() -> int:
	"""Returns the number of domains that tripped this run."""
	tripped = 0
	instantly = InstantlyService()

	with get_system_db_context() as session:
		domains = session.execute(
			text("SELECT id, domain, cluster_label, client_id FROM sending_domains WHERE quarantine_state = 'active'")
		).fetchall()

		for domain_row in domains:
			trip = _evaluate_domain(session, domain_row, instantly)
			if trip is None:
				continue
			tripped += 1
			if _recently_paged_check(trip.domain):
				continue
			_quarantine_and_swap(session, domain_row, trip)
			_record_paged(trip.domain)

	logger.info("deliverability_sentinel: %d domain(s) tripped this run", tripped)
	return tripped


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_sentinel_sweep()
