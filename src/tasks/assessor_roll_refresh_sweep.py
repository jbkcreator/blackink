"""Monthly assessor-roll refresh sweep (S-24, W2 §3.2.4 A).

For each configured county (Hillsborough, Pinellas — config/settings.py's
assessor_roll_path_*), reads the file at its configured local path (an
operator manually places/replaces this — see assessor_roll_loader.py's own
docstring for why this isn't a live scrape of an unverified vendor/county
URL), hands it to import_county_roll() for hash-based change detection and
a full per-county replace on real change, and records the outcome to
assessor_roll_imports — one row per county per tick, always, including a
successful no-op (an UNCHANGED row), so "the sweep ran and found nothing to
do" is as visible in the audit trail as "the sweep imported N rows".

Alerts #blackink-qa on:
  - MISSING: the configured path doesn't resolve to a readable file at all.
  - FAILED: the file was read but didn't parse/validate.
  - STALE: no SUCCESS for this county in over
    settings.assessor_roll_staleness_days (default 400 — county rolls are
    certified annually, so a healthy county should show one SUCCESS at
    least once a year).

Runs monthly via cron (scripts/crontab.txt), not an in-process
_start_background_workers thread — this repo's convention for
daily-or-less-frequent jobs (daily_digest, county_allocation_reassessment,
deliverability_sentinel all follow this split).

    python -m src.tasks.assessor_roll_refresh_sweep
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.assessor_roll_loader import ImportResult, import_county_roll
from src.services.slack.post import post_notice

logger = logging.getLogger(__name__)


def _read_file(path_str: str) -> Optional[bytes]:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.error("assessor_roll_refresh_sweep: failed to read %s: %s", path_str, exc)
        return None


def _record_import(db, county_slug: str, file_path: str, result: ImportResult) -> None:
    db.execute(
        text(
            "INSERT INTO assessor_roll_imports "
            "(county_slug, file_path, file_sha256, status, row_count, error) "
            "VALUES (:county_slug, :file_path, :file_sha256, :status, :row_count, :error)"
        ),
        {
            "county_slug": county_slug,
            "file_path": file_path,
            "file_sha256": result.file_sha256,
            "status": result.status,
            "row_count": result.row_count,
            "error": result.error,
        },
    )


def _check_staleness(db, county_slug: str, as_of: datetime, staleness_days: int) -> Optional[datetime]:
    """Returns the last SUCCESS's imported_at if it's older than the
    staleness threshold, else None. A county with NO prior SUCCESS at all
    also returns None here — that case is already fully covered by the
    MISSING/FAILED alert in _run_county, which fires every tick the
    problem persists; a distinct STALE alert on top of it would be
    redundant, and "no successful import since {as_of}" would misleadingly
    read as a recent import when there has never been one at all.
    Code-review fix: this function previously returned `as_of` itself for
    the never-imported case, causing exactly that redundant, misleadingly-
    worded double alert on every tick."""
    row = db.execute(
        text(
            "SELECT imported_at FROM assessor_roll_imports "
            "WHERE county_slug = :county_slug AND status = 'SUCCESS' "
            "ORDER BY imported_at DESC LIMIT 1"
        ),
        {"county_slug": county_slug},
    ).first()
    if row is None:
        return None
    last_success_at = row[0]
    if last_success_at.tzinfo is None:
        last_success_at = last_success_at.replace(tzinfo=timezone.utc)
    if as_of - last_success_at > timedelta(days=staleness_days):
        return last_success_at
    return None


async def _alert(text_body: str) -> None:
    await post_notice(channel_key="qa", text=text_body)


def _run_county(db, county_slug: str, file_path: str, as_of: datetime, staleness_days: int) -> str:
    file_bytes = _read_file(file_path)
    result = import_county_roll(db, county_slug, file_bytes)
    _record_import(db, county_slug, file_path, result)

    # Code-review fix: MISSING/FAILED already alert every tick the problem
    # persists — a STALE alert on the SAME tick was redundant noise for the
    # exact same underlying cause, not a distinct second problem.
    already_alerted_this_tick = False

    if result.status == "MISSING":
        asyncio.run(_alert(
            f":warning: *Assessor roll missing* — county=`{county_slug}` "
            f"path=`{file_path}` does not resolve to a readable file. "
            f"Win-Back for this county will keep resolving UNKNOWN until it's provisioned."
        ))
        already_alerted_this_tick = True
    elif result.status == "FAILED":
        asyncio.run(_alert(
            f":rotating_light: *Assessor roll import failed* — county=`{county_slug}` "
            f"error: {result.error}. Existing raw_assessor_parcels rows for this county are UNCHANGED."
        ))
        already_alerted_this_tick = True
    elif result.status == "SUCCESS":
        logger.info(
            "assessor_roll_refresh_sweep: county=%s imported %d row(s)", county_slug, result.row_count
        )

    if not already_alerted_this_tick:
        stale_since = _check_staleness(db, county_slug, as_of, staleness_days)
        if stale_since is not None:
            asyncio.run(_alert(
                f":hourglass: *Assessor roll stale* — county=`{county_slug}` has had no successful "
                f"import since `{stale_since.isoformat()}` (threshold: {staleness_days} days). "
                f"The county tax roll is certified annually — this county needs a refreshed extract."
            ))

    return result.status


def run_sweep(*, as_of: Optional[datetime] = None) -> dict[str, str]:
    as_of = as_of or datetime.now(timezone.utc)
    settings = get_settings()
    counties = [
        ("hillsborough_fl", settings.assessor_roll_path_hillsborough),
        ("pinellas_fl", settings.assessor_roll_path_pinellas),
    ]

    statuses: dict[str, str] = {}
    with get_system_db_context() as db:
        for county_slug, file_path in counties:
            try:
                statuses[county_slug] = _run_county(
                    db, county_slug, file_path, as_of, settings.assessor_roll_staleness_days
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("assessor_roll_refresh_sweep: county=%s tick failed", county_slug)
                statuses[county_slug] = "ERROR"

    logger.info("assessor_roll_refresh_sweep: %s", statuses)
    return statuses


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_sweep()
