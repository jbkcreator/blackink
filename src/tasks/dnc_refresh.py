"""Monthly DNC re-scrub via Tracerfy — deterministic, no LLM.

Keeps contacts.dnc_clean + contacts.dnc_checked_at fresh within the FTC's
31-day safe harbor window. Ported from FA's src/tasks/dnc_refresh.py, adapted
for Blackink's single contacts table (FA had owners/dbpr/subscribers split).

DNC status (dnc_clean=FALSE) is a legal eligibility flag — it does NOT
auto-suppress. Suppression is a separate opt-out action. The compliance gate
(compliance_gate.py DncProvider) reads dnc_clean; contacts with dnc_clean=FALSE
are blocked at send time but remain in the contacts table for record-keeping
and potential future re-check if the number leaves the registry.

API shape (Tracerfy /v1/api/):
  POST /dnc/scrub/  {"phones": [...]}  → {"dnc_queue_id": "..."}
  GET  /dnc/queue/{id}                 → {"pending": bool, "download_url": "..."}
  CSV columns: phone, national_dnc (Y/N), litigator (Y/N)

Cost: 1 Tracerfy credit (~$0.02) per phone checked.
Schedule: 1st of each month, 02:00 UTC.
  cron: 0 2 1 * * PYTHONPATH=. python -m src.tasks.dnc_refresh

Usage:
  PYTHONPATH=. python -m src.tasks.dnc_refresh --dry-run
  PYTHONPATH=. python -m src.tasks.dnc_refresh --limit 500
  PYTHONPATH=. python -m src.tasks.dnc_refresh --days 45
"""

import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.email_suppression import _normalize_phone
from src.services.tracerfy_client import BATCH_SIZE as _BATCH_SIZE
from src.services.tracerfy_client import is_dnc_hit, poll_queue, submit_batch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contact collection
# ---------------------------------------------------------------------------

def _collect_contacts(session, cutoff: datetime, limit: Optional[int]) -> list[tuple[int, str]]:
    """Return (contact_id, normalized_phone) for contacts needing DNC recheck.

    Eligible: has a phone, and either never checked or last checked before cutoff.
    Already opted-out contacts are skipped — they're blocked regardless of DNC status.
    """
    sql = text(
        "SELECT contact_id, phone FROM contacts "
        "WHERE phone IS NOT NULL "
        "  AND is_opted_out = FALSE "
        "  AND (dnc_checked_at IS NULL OR dnc_checked_at < :cutoff) "
        "ORDER BY dnc_checked_at ASC NULLS FIRST "
        + (f"LIMIT {int(limit)}" if limit else "")
    )
    rows = session.execute(sql, {"cutoff": cutoff}).fetchall()
    result = []
    for contact_id, raw_phone in rows:
        normalized = _normalize_phone(raw_phone)
        if normalized:
            result.append((contact_id, normalized))
        else:
            logger.warning("dnc_refresh: contact_id=%s phone=%r failed normalization — skipped",
                           contact_id, raw_phone)
    return result


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------

def _persist_results(
    session,
    csv_rows: list[dict],
    phone_to_contact_ids: dict[str, list[int]],
    stats: dict,
) -> None:
    now = datetime.now(timezone.utc)
    clean_ids: list[int] = []
    flagged_ids: list[int] = []

    for row in csv_rows:
        raw_phone = row.get("phone", "")
        phone = _normalize_phone(str(raw_phone).strip())
        if not phone:
            stats["unmatched"] += 1
            continue

        contact_ids = phone_to_contact_ids.get(phone)
        if not contact_ids:
            stats["unmatched"] += 1
            logger.warning("dnc_refresh: CSV phone=%s not in submitted batch", phone)
            continue

        if is_dnc_hit(row):
            flagged_ids.extend(contact_ids)
            stats["dnc_hits"] += 1
            logger.info("dnc_refresh: DNC hit phone=%s contact_ids=%s", phone, contact_ids)
        else:
            clean_ids.extend(contact_ids)
            stats["clean"] += 1

    # Two batched UPDATEs instead of one per CSV row.
    for dnc_clean, ids in ((True, clean_ids), (False, flagged_ids)):
        if ids:
            session.execute(
                text(
                    "UPDATE contacts "
                    "SET dnc_clean = :dnc_clean, dnc_checked_at = :now "
                    "WHERE contact_id = ANY(:ids)"
                ),
                {"dnc_clean": dnc_clean, "now": now, "ids": ids},
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_dnc_refresh(
    days: Optional[int] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Run monthly DNC re-scrub for all eligible contacts.

    Args:
        days: override DNC_RECHECK_DAYS from settings.
        dry_run: print scope without calling Tracerfy.
        limit: cap number of contacts processed (useful for testing).

    Returns:
        Stats dict: total, clean, dnc_hits, failed, unmatched, skipped.
    """
    settings = get_settings()

    if not settings.tracerfy_api_key:
        logger.warning("dnc_refresh: TRACERFY_API_KEY not set — skipped")
        return {"skipped": True, "reason": "TRACERFY_API_KEY not configured"}

    api_key = settings.tracerfy_api_key.get_secret_value()
    recheck_days = days if days is not None else settings.dnc_recheck_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=recheck_days)

    stats = {"total": 0, "clean": 0, "dnc_hits": 0, "failed": 0, "unmatched": 0, "skipped": False}

    with get_system_db_context() as session:
        contacts = _collect_contacts(session, cutoff, limit)

    stats["total"] = len(contacts)
    logger.info("dnc_refresh: %d contacts eligible (cutoff=%s)", len(contacts), cutoff.date())

    if not contacts:
        return stats

    if dry_run:
        logger.info("dnc_refresh: DRY RUN — would scrub %d contacts, no API call", len(contacts))
        return stats

    # Build phone → [contact_ids] map (multiple contacts can share a phone)
    phone_to_contact_ids: dict[str, list[int]] = {}
    for contact_id, phone in contacts:
        phone_to_contact_ids.setdefault(phone, []).append(contact_id)

    phones = list(phone_to_contact_ids.keys())

    for batch_start in range(0, len(phones), _BATCH_SIZE):
        batch = phones[batch_start: batch_start + _BATCH_SIZE]
        batch_num = batch_start // _BATCH_SIZE + 1
        logger.info("dnc_refresh: batch %d — submitting %d phones", batch_num, len(batch))

        try:
            queue_id = submit_batch(batch, api_key)
            csv_rows = poll_queue(queue_id, api_key)
        except Exception:
            logger.error("dnc_refresh: batch %d failed", batch_num, exc_info=True)
            stats["failed"] += len(batch)
            continue

        with get_system_db_context() as session:
            _persist_results(session, csv_rows, phone_to_contact_ids, stats)
            session.commit()

    logger.info(
        "dnc_refresh: done — total=%d clean=%d dnc_hits=%d failed=%d unmatched=%d",
        stats["total"], stats["clean"], stats["dnc_hits"], stats["failed"], stats["unmatched"],
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)

    parser = argparse.ArgumentParser(description="Monthly DNC re-scrub via Tracerfy")
    parser.add_argument("--days", type=int, default=None, help="Override DNC_RECHECK_DAYS")
    parser.add_argument("--limit", type=int, default=None, help="Max contacts to process")
    parser.add_argument("--dry-run", action="store_true", help="No API call, show scope only")
    args = parser.parse_args()

    try:
        result = run_dnc_refresh(days=args.days, dry_run=args.dry_run, limit=args.limit)
        sys.exit(0)
    except Exception as e:
        logger.error("dnc_refresh: fatal — %s", e)
        logger.debug(traceback.format_exc())
        sys.exit(1)
