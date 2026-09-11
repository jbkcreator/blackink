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
# Win-back re-scrub
# ---------------------------------------------------------------------------

def _collect_winback_rows(session, cutoff: datetime, limit: Optional[int]) -> list[tuple[int, str]]:
    """Return (winback_row_id, normalized_phone) for active win-back rows needing re-scrub.

    Eligible: outreach-eligible disposition, not stopped, not suppressed, has phone,
    and either never checked or last checked before cutoff. Rows that can never be
    contacted (SOLD/UNKNOWN or already stopped/suppressed) are skipped — same
    cost-avoidance reasoning as winback_ingest._run_dnc_scrub.
    """
    sql = text(
        "SELECT winback_row_id, phone FROM winback_rows "
        "WHERE phone IS NOT NULL "
        "  AND disposition IN ('STILL_OWNS_STILL_RENTING', 'STILL_OWNS_NOT_RENTING') "
        "  AND stopped_at IS NULL "
        "  AND suppression_state = FALSE "
        "  AND (dnc_checked_at IS NULL OR dnc_checked_at < :cutoff) "
        "ORDER BY dnc_checked_at ASC NULLS FIRST "
        + (f"LIMIT {int(limit)}" if limit else "")
    )
    rows = session.execute(sql, {"cutoff": cutoff}).fetchall()
    result = []
    for winback_row_id, raw_phone in rows:
        normalized = _normalize_phone(raw_phone)
        if normalized:
            result.append((winback_row_id, normalized))
        else:
            logger.warning(
                "dnc_refresh: winback_row_id=%s phone=%r failed normalization — skipped",
                winback_row_id, raw_phone,
            )
    return result


def _persist_winback_results(
    session,
    csv_rows: list[dict],
    phone_to_row_ids: dict[str, list[int]],
    stats: dict,
) -> None:
    """Write DNC results back to winback_rows.

    A hit sets suppression_state=TRUE (same column _run_dnc_scrub writes at
    ingest). A phone absent from the Tracerfy CSV is flagged as unverified —
    sets requires_human_review=TRUE, same fail-closed posture as _flag_unscrubbed.
    dnc_checked_at is updated on every row we got an answer for.
    """
    now = datetime.now(timezone.utc)
    hit_ids: list[int] = []
    clean_ids: list[int] = []
    answered_phones: set[str] = set()

    for row in csv_rows:
        phone = _normalize_phone(str(row.get("phone", "")).strip())
        if not phone:
            stats["wb_unmatched"] += 1
            continue
        row_ids = phone_to_row_ids.get(phone)
        if not row_ids:
            stats["wb_unmatched"] += 1
            logger.warning("dnc_refresh winback: CSV phone=%s not in submitted batch", phone)
            continue
        answered_phones.add(phone)
        if is_dnc_hit(row):
            hit_ids.extend(row_ids)
            stats["wb_dnc_hits"] += 1
            logger.info("dnc_refresh winback: DNC hit phone=%s row_ids=%s", phone, row_ids)
        else:
            clean_ids.extend(row_ids)
            stats["wb_clean"] += 1

    if hit_ids:
        session.execute(
            text(
                "UPDATE winback_rows "
                "SET suppression_state = TRUE, suppression_reason = 'DNC_LISTED', "
                "    dnc_clean = FALSE, dnc_checked_at = :now, updated_at = :now "
                "WHERE winback_row_id = ANY(:ids)"
            ),
            {"now": now, "ids": hit_ids},
        )
    if clean_ids:
        session.execute(
            text(
                "UPDATE winback_rows "
                "SET dnc_clean = TRUE, dnc_checked_at = :now, updated_at = :now "
                "WHERE winback_row_id = ANY(:ids)"
            ),
            {"now": now, "ids": clean_ids},
        )

    # Rows whose phone was submitted but absent from the CSV — fail closed.
    unanswered_ids: list[int] = []
    for phone, row_ids in phone_to_row_ids.items():
        if phone not in answered_phones:
            unanswered_ids.extend(row_ids)
    if unanswered_ids:
        session.execute(
            text(
                "UPDATE winback_rows "
                "SET requires_human_review = TRUE, updated_at = :now "
                "WHERE winback_row_id = ANY(:ids)"
            ),
            {"now": now, "ids": unanswered_ids},
        )
        stats["wb_unverified"] += len(unanswered_ids)
        logger.warning(
            "dnc_refresh winback: %d rows absent from Tracerfy CSV — flagged requires_human_review",
            len(unanswered_ids),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_dnc_refresh(
    days: Optional[int] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Run monthly DNC re-scrub for all eligible contacts AND active win-back rows.

    Win-back rows are re-scrubbed on the same cadence as contacts — a number
    added to the DNC registry after the row's ingest-time scrub must be caught
    before Touch 2/3, not only at import time.

    Args:
        days: override DNC_RECHECK_DAYS from settings.
        dry_run: print scope without calling Tracerfy.
        limit: cap number of contacts processed (applied separately to each table).

    Returns:
        Stats dict with contact and win-back counters.
    """
    settings = get_settings()

    if not settings.tracerfy_api_key:
        logger.warning("dnc_refresh: TRACERFY_API_KEY not set — skipped")
        return {"skipped": True, "reason": "TRACERFY_API_KEY not configured"}

    api_key = settings.tracerfy_api_key.get_secret_value()
    recheck_days = days if days is not None else settings.dnc_recheck_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=recheck_days)

    stats = {
        "total": 0, "clean": 0, "dnc_hits": 0, "failed": 0, "unmatched": 0, "skipped": False,
        "wb_total": 0, "wb_clean": 0, "wb_dnc_hits": 0, "wb_failed": 0,
        "wb_unmatched": 0, "wb_unverified": 0,
    }

    with get_system_db_context() as session:
        contacts = _collect_contacts(session, cutoff, limit)
        winback_rows = _collect_winback_rows(session, cutoff, limit)

    stats["total"] = len(contacts)
    stats["wb_total"] = len(winback_rows)
    logger.info(
        "dnc_refresh: %d contacts, %d win-back rows eligible (cutoff=%s)",
        len(contacts), len(winback_rows), cutoff.date(),
    )

    if not contacts and not winback_rows:
        return stats

    if dry_run:
        logger.info(
            "dnc_refresh: DRY RUN — would scrub %d contacts + %d win-back rows, no API call",
            len(contacts), len(winback_rows),
        )
        return stats

    # ── Contacts ──────────────────────────────────────────────────────────────
    if contacts:
        phone_to_contact_ids: dict[str, list[int]] = {}
        for contact_id, phone in contacts:
            phone_to_contact_ids.setdefault(phone, []).append(contact_id)

        phones = list(phone_to_contact_ids.keys())
        for batch_start in range(0, len(phones), _BATCH_SIZE):
            batch = phones[batch_start: batch_start + _BATCH_SIZE]
            batch_num = batch_start // _BATCH_SIZE + 1
            logger.info("dnc_refresh contacts: batch %d — submitting %d phones", batch_num, len(batch))
            try:
                queue_id = submit_batch(batch, api_key)
                csv_rows = poll_queue(queue_id, api_key)
            except Exception:
                logger.error("dnc_refresh contacts: batch %d failed", batch_num, exc_info=True)
                stats["failed"] += len(batch)
                continue
            with get_system_db_context() as session:
                _persist_results(session, csv_rows, phone_to_contact_ids, stats)
                session.commit()

    # ── Win-back rows ─────────────────────────────────────────────────────────
    if winback_rows:
        phone_to_row_ids: dict[str, list[int]] = {}
        for row_id, phone in winback_rows:
            phone_to_row_ids.setdefault(phone, []).append(row_id)

        wb_phones = list(phone_to_row_ids.keys())
        for batch_start in range(0, len(wb_phones), _BATCH_SIZE):
            batch = wb_phones[batch_start: batch_start + _BATCH_SIZE]
            batch_num = batch_start // _BATCH_SIZE + 1
            logger.info("dnc_refresh winback: batch %d — submitting %d phones", batch_num, len(batch))
            try:
                queue_id = submit_batch(batch, api_key)
                csv_rows = poll_queue(queue_id, api_key)
            except Exception:
                logger.error("dnc_refresh winback: batch %d failed", batch_num, exc_info=True)
                stats["wb_failed"] += len(batch)
                continue
            with get_system_db_context() as session:
                _persist_winback_results(session, csv_rows, phone_to_row_ids, stats)
                session.commit()

    logger.info(
        "dnc_refresh: done — "
        "contacts: total=%d clean=%d hits=%d failed=%d unmatched=%d | "
        "winback: total=%d clean=%d hits=%d failed=%d unmatched=%d unverified=%d",
        stats["total"], stats["clean"], stats["dnc_hits"], stats["failed"], stats["unmatched"],
        stats["wb_total"], stats["wb_clean"], stats["wb_dnc_hits"],
        stats["wb_failed"], stats["wb_unmatched"], stats["wb_unverified"],
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
