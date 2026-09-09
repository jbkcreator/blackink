"""Owner enrichment (skip-trace) verification sweep — Subtask 3.2.1.

Client source: blackink-client-comments-04-09-2026.md:77 — "Confirm the
enrichment step (owner -> phone/email) for every signal... If it isn't, it's
a blocker for everything in §3." This is the sweep that clears
winback_rows.enrichment_timestamp before evaluate_winback_touch_gate or the
/arm endpoint (src/api/winback_router.py) will allow a row to be sequenced.

Runs under blackink_system (BYPASSRLS), same posture as promotion_sweep.py —
NEVER import this from src/api/.

    PYTHONPATH=. python -m src.tasks.enrichment_verification --client-id <id> [--import-id <id>] [--limit 10]

Sweep, self-heal -> claim -> enrich -> scrub -> log -> post, same ordering
meeting_outcome_prompt_sender.py already uses:

  1. Self-heal: a row whose enrichment_attempts has hit
     owner_enrichment_max_attempts without ever getting an answer is
     terminally marked (enrichment_timestamp=NOW(),
     requires_enrichment_review=TRUE) — otherwise it stays invisible to a
     plain audit SELECT and the gate reports "enrichment has not run" for a
     row genuinely tried three times. See src/services/winback_sequencer.py's
     _check_enrichment for the gate side of this.
  2. Claim: outreach-eligible, not suppressed, not stopped, not yet
     enriched, attempts under budget, not currently leased by another sweep
     (see _CLAIM_LEASE_MINUTES — PR review finding, confirmed real: the
     FOR UPDATE row lock alone does not survive the up-to-10-minute
     submit+poll window, so a durable lease is what actually prevents a
     concurrent sweep from re-claiming and re-submitting the same rows).
     Ordered disposition-first (3.1.2's STILL_OWNS_STILL_RENTING priority),
     then audit_loss_dollars_est DESC NULLS LAST — a proxy for the Unified
     System Specification's "targeting top-tier scores" (that line's own
     "scores" is the deed engine's Owner Score, which winback_rows doesn't
     carry; this is the closest available stand-in, not a literal
     implementation of that line). The lease itself is stamped and
     committed immediately after claiming, before any vendor call, closing
     the window completely.
  3. Enrich, one vendor-batch-sized chunk at a time (see
     src/services/owner_enrichment.py's OwnerEnrichmentProvider.submit()/
     collect() split for why chunking-with-a-commit-in-between is required,
     not incidental).
  4. DNC-scrub any newly-discovered phone through
     winback_ingest.dnc_scrub_rows() — preserves 3.1.1's "DNC scrub before
     any sequence can arm" ordering for numbers that didn't exist at import
     time.
  5. Log one owner_enrichment_completed event per signal (including
     self-heal's own terminal marks).
  6. Post the #blackink-qa verification summary — printed to stdout too,
     and the task exits non-zero if the Slack post itself didn't succeed, so
     an operator running this as the pre-pilot gate cannot mistake a silent
     Slack no-op for a pass.

Submit-stage vs collect-stage vendor failures are handled differently, and
this matters for enrichment_attempts correctness — see
_enrich_claimed_rows()'s own docstring.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services import owner_enrichment as oe
from src.services.events import log_event
from src.services.slack.post import post_notice
from src.services.winback_ingest import dnc_scrub_rows

logger = logging.getLogger(__name__)

# How long a row stays excluded from re-claim after this sweep claims it,
# before its own enrichment_timestamp is set — comfortably over Tracerfy's
# ~10-minute poll budget (tracerfy_client._TRACE_MAX_POLL_ATTEMPTS *
# _TRACE_POLL_INTERVAL_SEC), same naming convention as
# self_serve_audit_worker.py's own _CLAIM_LEASE_MINUTES. A row whose lease
# expires without completing (a genuine crash mid-poll, not ordinary
# concurrent execution) is re-claimed and freshly re-submitted — not
# resumed via its stored enrichment_queue_id. Deliberate scope decision:
# resuming an abandoned poll needs reconstructing a submit handle and
# re-deriving match keys, real added complexity for a case already bounded
# by this timeout to a rare, occasional duplicate submission on crash —
# the same tradeoff self_serve_audit_worker.py's own lease already accepts.
_CLAIM_LEASE_MINUTES = 15


def _self_heal_exhausted_rows(session: Session, client_id: str, max_attempts: int, now: datetime) -> int:
    """Terminally marks any row whose enrichment_attempts has reached
    max_attempts without ever getting an answer. Runs BEFORE the claim step
    so an exhausted row is visible to an audit query and correctly excluded
    from the claim query in the SAME sweep it becomes exhausted, not one
    cycle later."""
    rows = session.execute(
        text(
            "UPDATE winback_rows "
            "SET enrichment_timestamp = :now, "
            "    enrichment_provider = COALESCE(enrichment_provider, 'none') || ' (attempts_exhausted)', "
            "    requires_enrichment_review = TRUE, "
            "    updated_at = :now "
            "WHERE client_id = :client_id "
            "  AND enrichment_timestamp IS NULL "
            "  AND enrichment_attempts >= :max_attempts "
            "RETURNING winback_row_id, enrichment_provider"
        ),
        {"now": now, "client_id": client_id, "max_attempts": max_attempts},
    ).fetchall()
    for row in rows:
        log_event(
            client_id,
            "owner_enrichment_completed",
            entity_type="winback_row",
            entity_id=str(row.winback_row_id),
            payload={
                "provider": row.enrichment_provider,
                "signal_source": "WINBACK_IMPORT",
                "email_found": False,
                "phone_found": False,
                "requires_review": True,
            },
            session=session,
        )
    return len(rows)


def _claim_rows(session: Session, client_id: str, import_id: Optional[str], max_attempts: int, limit: int, now: datetime) -> list:
    """FOR UPDATE SKIP LOCKED — same convention as every other job sweep in
    this repo (show_rate_reminders.py, calendar_confirmation.py). The
    disposition filter bounds vendor spend the same way winback_ingest.py's
    DNC branch does: a SOLD/UNKNOWN row can never be sequenced, so
    skip-tracing it burns money for nothing.

    The enrichment_claimed_at lease exclusion (PR review finding, confirmed
    real) is what actually prevents a concurrent sweep from re-claiming a
    row this same query already claimed: the FOR UPDATE row lock is
    released as soon as run_sweep() commits the lease stamp (see there) —
    without a durable lease, the row would still be enrichment_timestamp IS
    NULL and enrichment_attempts under budget for the entire submit+poll
    duration (up to ~10 minutes for a real Tracerfy call), fully re-claimable
    by another sweep instance the moment the lock is gone."""
    lease_cutoff = now - timedelta(minutes=_CLAIM_LEASE_MINUTES)
    import_filter = "AND import_id = :import_id " if import_id else ""
    rows = session.execute(
        text(
            "SELECT winback_row_id, owner_name, property_address_raw, county_slug, email, phone "
            "FROM winback_rows "
            "WHERE client_id = :client_id "
            + import_filter
            + "  AND disposition IN ('STILL_OWNS_STILL_RENTING', 'STILL_OWNS_NOT_RENTING') "
            "  AND suppression_state = FALSE AND stopped_at IS NULL "
            "  AND enrichment_timestamp IS NULL AND enrichment_attempts < :max_attempts "
            "  AND (enrichment_claimed_at IS NULL OR enrichment_claimed_at < :lease_cutoff) "
            "ORDER BY CASE disposition WHEN 'STILL_OWNS_STILL_RENTING' THEN 0 ELSE 1 END, "
            "         audit_loss_dollars_est DESC NULLS LAST, winback_row_id "
            "LIMIT :limit FOR UPDATE SKIP LOCKED"
        ),
        {"client_id": client_id, "import_id": import_id, "max_attempts": max_attempts, "limit": limit, "lease_cutoff": lease_cutoff},
    ).fetchall()
    return list(rows)


def _mark_claimed(session: Session, winback_row_ids: list[int], now: datetime) -> None:
    """Stamps the lease on a just-claimed batch, in the SAME transaction
    that still holds _claim_rows's FOR UPDATE SKIP LOCKED row lock — commit
    this before doing anything else (submitting to the vendor, polling) so
    there is no window where a row is both unlocked and unleased. See
    _claim_rows's own docstring for the failure this closes."""
    session.execute(
        text("UPDATE winback_rows SET enrichment_claimed_at = :now WHERE winback_row_id = ANY(:ids)"),
        {"ids": winback_row_ids, "now": now},
    )


def _count_pending_backlog(session: Session, client_id: str, import_id: Optional[str], max_attempts: int) -> int:
    """Same predicate as _claim_rows, minus the LIMIT — the DoD's own
    "summary showing counts of enriched / failed / pending" requires
    `pending` to reflect real backlog beyond one sweep's --limit, not just
    in-flight failures within the claimed batch. Without this, a 500-row
    import swept with --limit 10 would report pending=0 the moment those 10
    rows are processed, even though 490 are still waiting — a misleading
    summary for the exact pre-pilot gate this task exists to be."""
    import_filter = "AND import_id = :import_id " if import_id else ""
    count = session.execute(
        text(
            "SELECT COUNT(*) FROM winback_rows "
            "WHERE client_id = :client_id "
            + import_filter
            + "  AND disposition IN ('STILL_OWNS_STILL_RENTING', 'STILL_OWNS_NOT_RENTING') "
            "  AND suppression_state = FALSE AND stopped_at IS NULL "
            "  AND enrichment_timestamp IS NULL AND enrichment_attempts < :max_attempts"
        ),
        {"client_id": client_id, "import_id": import_id, "max_attempts": max_attempts},
    ).scalar_one()
    return int(count)


def _bump_attempts(session: Session, winback_row_ids: list[int], now: datetime, queue_id: Optional[str] = None) -> None:
    """queue_id is stored for operational visibility only (checking the
    vendor's own dashboard for a stuck batch) — nothing in this codebase
    resumes an abandoned poll from it; see _CLAIM_LEASE_MINUTES's own
    comment for that scope decision. Stub/not-submitted chunks pass None,
    leaving the column untouched via COALESCE rather than overwriting a
    prior value with NULL."""
    session.execute(
        text(
            "UPDATE winback_rows SET enrichment_attempts = enrichment_attempts + 1, "
            "enrichment_queue_id = COALESCE(:queue_id, enrichment_queue_id), updated_at = :now "
            "WHERE winback_row_id = ANY(:ids)"
        ),
        {"ids": winback_row_ids, "now": now, "queue_id": queue_id},
    )


def _enrich_claimed_rows(
    session: Session,
    client_id: str,
    rows: list,
    provider: oe.OwnerEnrichmentProvider,
    settings,
    counts: dict,
) -> None:
    """Enriches one claimed set, chunked to the vendor's own batch size.

    Submit-stage vs collect-stage failures are handled differently — NOT a
    style choice, this is what keeps enrichment_attempts an honest budget:

      - A chunk's submit() fails (bad key, rate limit, malformed request):
        nothing was queued, nothing plausibly billed. enrichment_attempts is
        NOT bumped for this chunk's rows — they are cleanly re-claimable
        next sweep with no wasted retry budget. Logged as an error and
        counted in counts["submit_failures"] so the caller can make the run
        exit non-zero (loud, without aborting every other chunk — same
        per-batch try/continue posture src/tasks/dnc_refresh.py's own sweep
        already uses, rather than crashing the whole sweep on one bad
        chunk).
      - A chunk's submit() succeeds: enrichment_attempts IS bumped and
        committed for this chunk's rows BEFORE calling collect() — the
        vendor may already be billing/processing, so a crash during the
        (up to 10-minute) poll inside collect() must not risk a silent
        re-submit-and-re-charge of the same batch on the next sweep.
      - collect() then fails: rows stay enrichment_timestamp IS NULL with
        the bump already applied — re-claimed next sweep, eventually
        reaching the self-heal step once attempts are exhausted.
    """
    from src.services.tracerfy_client import BATCH_SIZE

    inputs = [
        oe.EnrichmentInput(
            winback_row_id=r.winback_row_id,
            owner_name=r.owner_name,
            property_address=r.property_address_raw,
            county_slug=r.county_slug,
            known_email=r.email,
            known_phone=r.phone,
        )
        for r in rows
    ]
    now = datetime.now(timezone.utc)
    dnc_targets: list[tuple[int, str]] = []

    for chunk in oe.chunk_inputs(inputs, BATCH_SIZE):
        chunk_ids = [i.winback_row_id for i in chunk]
        try:
            handle = provider.submit(chunk)
        except Exception:
            logger.error(
                "enrichment_verification: submit failed for %d row(s) — no attempt spent, will retry next sweep",
                len(chunk), exc_info=True,
            )
            counts["submit_failures"] = counts.get("submit_failures", 0) + 1
            continue

        # getattr, not an OwnerEnrichmentProvider ABC method — queue_id is
        # Tracerfy-specific diagnostic data (StubOwnerEnrichmentProvider's
        # handle is just a plain list, with no such attribute), and this
        # avoids adding an abstract method to the interface for one
        # optional piece of operational metadata.
        queue_id = getattr(handle, "queue_id", None) or None
        with session.begin_nested():
            _bump_attempts(session, chunk_ids, now, queue_id=queue_id)
        session.commit()

        try:
            results = provider.collect(handle)
        except Exception:
            logger.error(
                "enrichment_verification: collect failed for %d row(s) — attempt already spent, re-claimable",
                len(chunk), exc_info=True,
            )
            continue

        # getattr, not an ABC method — same optional-metadata pattern as
        # queue_id above. StubOwnerEnrichmentProvider's handle (a plain
        # list) has no such attribute and correctly falls back to empty:
        # the stub never fails to parse an address, it just passes CSV
        # values through (see StubOwnerEnrichmentProvider's own docstring).
        unprocessable_ids = getattr(handle, "unprocessable_ids", frozenset())

        for i in chunk:
            result = results.get(i.winback_row_id)
            unprocessable = i.winback_row_id in unprocessable_ids
            with session.begin_nested():
                if result is None:
                    # Not-found (vendor was asked, returned nothing) or
                    # unprocessable (address never parsed, vendor was never
                    # asked at all) — both produce a synthetic result here
                    # so requires_enrichment_review and the event log come
                    # from the same one place, but apply_result's
                    # unprocessable flag keeps them from being treated as
                    # equivalent (PR review finding, confirmed real: a
                    # pre-existing CSV email must not let an unprocessable
                    # row pass the /arm gate as if enrichment had run).
                    reason = "unprocessable_address" if unprocessable else "not_found"
                    result = oe.EnrichmentResult(
                        email=None, email_status="UNVERIFIED", phone=None,
                        phone_verified=None, provider=f"{_provider_name(provider)} ({reason})",
                    )
                outcome = oe.apply_result(session, i.winback_row_id, result, now, unprocessable=unprocessable)
                if outcome is None:
                    logger.error("enrichment_verification: winback_row_id=%s vanished mid-sweep — skipped", i.winback_row_id)
                    continue

                if outcome.newly_discovered_phone:
                    dnc_targets.append((i.winback_row_id, outcome.newly_discovered_phone))

                if outcome.requires_enrichment_review:
                    counts["failed"] = counts.get("failed", 0) + 1
                else:
                    counts["enriched"] = counts.get("enriched", 0) + 1

                log_event(
                    client_id,
                    "owner_enrichment_completed",
                    entity_type="winback_row",
                    entity_id=str(i.winback_row_id),
                    payload={
                        "provider": result.provider,
                        "signal_source": "WINBACK_IMPORT",
                        "email_found": bool(outcome.email),
                        "phone_found": bool(outcome.phone),
                        "requires_review": outcome.requires_enrichment_review,
                    },
                    session=session,
                )
        session.commit()

    if dnc_targets:
        dnc_counts = {"suppressed_count": 0}
        dnc_scrub_rows(session, dnc_targets, settings, dnc_counts)
        session.commit()


def _provider_name(provider: oe.OwnerEnrichmentProvider) -> str:
    return "stub" if isinstance(provider, oe.StubOwnerEnrichmentProvider) else "tracerfy"


def run_sweep(client_id: str, import_id: Optional[str] = None, limit: Optional[int] = None) -> dict:
    settings = get_settings()
    max_attempts = settings.owner_enrichment_max_attempts
    limit = limit if limit is not None else settings.owner_enrichment_max_per_run

    provider: oe.OwnerEnrichmentProvider
    if settings.tracerfy_api_key:
        provider = oe.TracerfyEnrichmentProvider(settings.tracerfy_api_key.get_secret_value())
    else:
        logger.warning("enrichment_verification: TRACERFY_API_KEY not set — using StubOwnerEnrichmentProvider")
        provider = oe.StubOwnerEnrichmentProvider()

    counts: dict = {"enriched": 0, "failed": 0, "pending": 0, "attempts_exhausted": 0, "submit_failures": 0}
    counts["provider"] = _provider_name(provider)

    with get_system_db_context() as session:
        now = datetime.now(timezone.utc)
        counts["attempts_exhausted"] = _self_heal_exhausted_rows(session, client_id, max_attempts, now)
        session.commit()

        rows = _claim_rows(session, client_id, import_id, max_attempts, limit, now)
        if rows:
            # Stamp the lease and commit BEFORE any vendor call — this is
            # what closes the concurrency gap (PR review finding): it keeps
            # the row lease durable across the very moment the original
            # FOR UPDATE row lock is released, so no window exists where a
            # row is both unlocked and unleased for a second sweep to claim.
            _mark_claimed(session, [r.winback_row_id for r in rows], now)
            session.commit()
            _enrich_claimed_rows(session, client_id, rows, provider, settings, counts)

        # Homestead-drop / same-owner hook (client comments:28-29) — no-op
        # today (Week2_Tasks_Dev_Split_v1.md:11 defers both signal types),
        # called here so it is on the real sweep path, not orphaned.
        oe.enrich_homestead_drop_signals(session, provider)

        # Authoritative, not incremental — computed once here rather than
        # accumulated inside _enrich_claimed_rows, so a submit/collect
        # failure's rows are counted exactly once regardless of how the
        # claimed batch was chunked.
        counts["pending"] = _count_pending_backlog(session, client_id, import_id, max_attempts)

    return counts


async def _post_summary(counts: dict) -> bool:
    ts = await post_notice(channel_key="qa", text=oe.build_qa_summary(counts))
    return ts is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="Owner enrichment (skip-trace) verification sweep")
    parser.add_argument("--client-id", required=True, help="Never defaulted — an unscoped sweep must not silently no-op")
    parser.add_argument("--import-id", default=None, help="Restrict to one winback_imports batch")
    parser.add_argument("--limit", type=int, default=None, help="Overrides OWNER_ENRICHMENT_MAX_PER_RUN")
    args = parser.parse_args()

    counts = run_sweep(client_id=args.client_id, import_id=args.import_id, limit=args.limit)
    summary_text = oe.build_qa_summary(counts)
    print(summary_text)

    posted = asyncio.run(_post_summary(counts))
    if not posted:
        print("enrichment_verification: #blackink-qa post did NOT succeed (channel unconfigured, or bot not in channel) — see logs")

    if not posted or counts.get("submit_failures"):
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
