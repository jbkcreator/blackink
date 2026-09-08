"""Owner enrichment (skip-trace) — Subtask 3.2.1, Enrichment Pipeline
Wiring Verification.

Client source: blackink-client-comments-04-09-2026.md:77 — "Confirm the
enrichment step (owner -> phone/email) for every signal, including the new
homestead and same-owner rows. If it isn't, it's a blocker for everything in
§3." The arrow is directional: this module PRODUCES a contact method from an
owner's name + property address — it is not a validation pass over contact
details someone else already supplied.

Batch-shaped, not per-row — this is not a style choice, it is forced by the
chosen vendor (Tracerfy, client decision 2026-09-08, same account as DNC).
Tracerfy's API is submit-then-poll (src/services/tracerfy_client.py:
submit_batch/poll_queue, up to 10 minutes) — compliance_gate.py's own
DncProvider docstring already documents at length why that shape cannot sit
behind a per-contact synchronous interface. OwnerEnrichmentProvider mirrors
that lesson: one submit()/collect() call pair per vendor-sized batch, never
one call per row.

Only caller: src/tasks/enrichment_verification.py. See
docs/plans/2026-09-08-subtask-3.2.1-enrichment-pipeline-wiring-verification.md
for the full design rationale.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.email_suppression import _normalize_phone

logger = logging.getLogger(__name__)

# Same vocabulary as contacts.email_status (apply_contacts.py) — deliberately
# not a new one.
_EMAIL_STATUSES = ("VERIFIED", "ESTIMATED", "UNVERIFIED", "BOUNCED")


@dataclass(frozen=True)
class EnrichmentInput:
    """One row's worth of what submit()/collect() need to look an owner up.
    known_email/known_phone are the CSV's own "last known" values (may be
    None) — a provider is free to use them as hints or ignore them; either
    way the caller never assumes the provider echoes them back unchanged."""

    winback_row_id: int
    owner_name: str
    property_address: str
    county_slug: Optional[str]
    known_email: Optional[str]
    known_phone: Optional[str]


@dataclass(frozen=True)
class EnrichmentResult:
    email: Optional[str]
    email_status: str  # one of _EMAIL_STATUSES
    phone: Optional[str]
    phone_verified: Optional[bool]  # None = provider could not determine
    provider: str  # goes into the owner_enrichment_completed event payload — never blank

    def __post_init__(self) -> None:
        if self.email_status not in _EMAIL_STATUSES:
            raise ValueError(f"EnrichmentResult.email_status={self.email_status!r} not in {_EMAIL_STATUSES}")
        if not self.provider:
            raise ValueError("EnrichmentResult.provider must not be blank")
        if self.phone_verified is True and not self.phone:
            # Defense in depth: the Tracerfy skip-trace response shape is
            # still unconfirmed (see tracerfy_client.submit_skiptrace_batch's
            # own docstring), so a provider claiming "verified" with no phone
            # value is rejected here rather than silently accepted and later
            # misread by apply_result as verifying whatever phone the row
            # already had before this call.
            raise ValueError("EnrichmentResult.phone_verified=True requires a non-empty phone")


class OwnerEnrichmentProvider(ABC):
    """Split into submit()/collect() rather than one opaque enrich_batch()
    call — NOT a style choice, this split is what makes the crash-safety
    design in src/tasks/enrichment_verification.py possible.

    A submit-stage failure (bad key, rate limit, malformed request) means
    the vendor never accepted the batch — nothing queued, nothing plausibly
    billed, so the caller must NOT spend any row's bounded retry budget on
    it. A collect-stage failure happens strictly AFTER submission succeeded
    — the vendor has the batch and may be billing/processing it, so a crash
    here (process killed mid-poll) must not let the same batch be silently
    re-submitted and re-charged on the next sweep run. The caller commits
    each row's enrichment_attempts bump between submit() succeeding and
    calling collect() — exactly the boundary this split exists to expose.
    See the task's own module docstring for the full rationale."""

    @abstractmethod
    def submit(self, inputs: list[EnrichmentInput]) -> object:
        """Submit one batch (already chunked to this provider's own batch
        size limit by the caller — see chunk_inputs()). Returns an opaque
        handle to pass to collect(). Raises ONLY for a submit-stage failure
        — callers must not count this as a spent attempt for any row in the
        batch."""

    @abstractmethod
    def collect(self, handle: object) -> dict[int, EnrichmentResult]:
        """Block until the submitted batch's results are ready (may take up
        to 10 minutes for a real vendor's poll) and return them, keyed by
        winback_row_id. A row_id ABSENT from the returned map means the
        provider returned nothing for it — callers must treat that as
        not-found, never as an error and never as clean. Raises ONLY for a
        failure AFTER submission already succeeded — callers must treat this
        as a spent attempt for every row in the batch, since the vendor may
        already be processing/billing it."""


class StubOwnerEnrichmentProvider(OwnerEnrichmentProvider):
    """No skip-trace call — passes each row's own CSV values straight
    through unchanged. Two properties are load-bearing and must not be
    "improved":

      - Never invents a contact method and never returns VERIFIED. Same
        posture as compliance_gate.StubDncProvider returning None: never
        guess clean on missing data.
      - Passes known_email/known_phone through unchanged, so a row that
        already had a CSV email keeps exactly the sequenceability it has
        today when this stub is in play — Subtask 3.1.2 does not regress
        the moment the enrichment gate check lands. A stub that returned
        email=None here would block every win-back send in the system.

    This is the fallback when no TRACERFY_API_KEY is set (see
    config/settings.py's tracerfy_api_key) and the shape the test suite's
    seeded fakes subclass. submit()/collect() never raise — there is no
    vendor call to fail, so the submit-vs-collect crash-safety split this
    interface exists for is simply a no-op here."""

    def submit(self, inputs: list[EnrichmentInput]) -> object:
        return inputs

    def collect(self, handle: object) -> dict[int, EnrichmentResult]:
        return {
            i.winback_row_id: EnrichmentResult(
                email=i.known_email,
                email_status="UNVERIFIED",
                phone=i.known_phone,
                phone_verified=None,
                provider="stub",
            )
            for i in handle
        }


@dataclass(frozen=True)
class _TracerfySubmitHandle:
    """What Tracerfy's submit() returns to feed collect(): the queue_id to
    poll, plus the exact input batch (needed by collect() to know which
    winback_row_ids to expect back)."""

    queue_id: str
    inputs: list[EnrichmentInput]


class TracerfyEnrichmentProvider(OwnerEnrichmentProvider):
    """Real skip-trace provider — same Tracerfy account as the DNC scrub
    (config/settings.py's tracerfy_api_key). Thin adapter over
    tracerfy_client's submit_skiptrace_batch()/poll_queue(): this class owns
    row_id keying and EnrichmentResult parsing, tracerfy_client owns HTTP
    transport — same split as compliance_gate.DncProvider vs tracerfy_client
    for DNC.

    submit() will raise NotImplementedError until
    tracerfy_client.submit_skiptrace_batch() is implemented against
    Tracerfy's real, confirmed API contract — see that function's own
    docstring. This class's own logic (batching via chunk_inputs(), row-id
    matching, result parsing) is complete and ready; only the HTTP call
    beneath submit() is deliberately unfinished.

    Caller (src/tasks/enrichment_verification.py) MUST call submit() for one
    chunk at a time (chunk_inputs()-sized, never the whole claimed set in one
    call) and commit that chunk's enrichment_attempts bump before calling
    collect() on the resulting handle — that ordering is what makes a
    submit-stage failure cost zero retry budget while a collect-stage
    (poll) failure still protects against a mid-poll crash re-charging the
    same batch. This class does not enforce that ordering itself; it is a
    caller contract, documented on OwnerEnrichmentProvider's own ABC."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def submit(self, inputs: list[EnrichmentInput]) -> object:
        from src.services.tracerfy_client import submit_skiptrace_batch

        payload = [
            {
                "winback_row_id": i.winback_row_id,
                "owner_name": i.owner_name,
                "property_address": i.property_address,
                "county_slug": i.county_slug,
            }
            for i in inputs
        ]
        queue_id = submit_skiptrace_batch(payload, self._api_key)
        return _TracerfySubmitHandle(queue_id=queue_id, inputs=inputs)

    def collect(self, handle: object) -> dict[int, EnrichmentResult]:
        from src.services.tracerfy_client import poll_queue

        assert isinstance(handle, _TracerfySubmitHandle)
        # NOTE: the result-CSV column names read here (winback_row_id,
        # email, email_status, phone, phone_verified) are a PLACEHOLDER
        # shape — they must be confirmed against Tracerfy's real skip-trace
        # response before this parsing is trusted. submit() above raises
        # NotImplementedError today, so this method is never actually
        # reached against unverified column names yet.
        csv_rows = poll_queue(handle.queue_id, self._api_key)
        by_row_id = {int(r["winback_row_id"]): r for r in csv_rows if r.get("winback_row_id")}
        result: dict[int, EnrichmentResult] = {}
        for i in handle.inputs:
            row = by_row_id.get(i.winback_row_id)
            if row is None:
                continue  # not-found — caller (apply_result) treats an absent key correctly
            result[i.winback_row_id] = EnrichmentResult(
                email=(row.get("email") or "").strip() or None,
                email_status="ESTIMATED",
                phone=(row.get("phone") or "").strip() or None,
                phone_verified=_parse_bool(row.get("phone_verified")),
                provider="tracerfy",
            )
        return result


def _parse_bool(value) -> Optional[bool]:
    s = str(value or "").strip().upper()
    if s in ("Y", "YES", "TRUE", "1"):
        return True
    if s in ("N", "NO", "FALSE", "0"):
        return False
    return None


def chunk_inputs(inputs: list[EnrichmentInput], batch_size: int) -> list[list[EnrichmentInput]]:
    """Split at batch_size — the caller (src/tasks/enrichment_verification.py)
    must submit one vendor-batch-sized chunk at a time, each with its own
    submit()/collect() call pair and its own attempts-bump commit in between
    (see OwnerEnrichmentProvider's docstring for why); this is a pure
    splitting helper, no I/O."""
    return [inputs[i: i + batch_size] for i in range(0, len(inputs), batch_size)]


@dataclass(frozen=True)
class ApplyOutcome:
    """What apply_result actually computed, so a caller building the
    owner_enrichment_completed event payload / counting enriched-vs-failed
    doesn't need a second round-trip SELECT to re-read what this function
    just wrote."""

    newly_discovered_phone: Optional[str]  # normalized, not yet on the row before this call — None if none
    requires_enrichment_review: bool
    email: Optional[str]  # the row's email AFTER this call (existing value if the provider found nothing)
    phone: Optional[str]  # the row's phone AFTER this call, same rule


def apply_result(session: Session, winback_row_id: int, result: EnrichmentResult, now: datetime) -> Optional[ApplyOutcome]:
    """Persists one row's enrichment result. Returns None only if the row
    itself was not found (logged as an error — should not happen under
    normal operation, since the caller always claimed the row first).

    Never overwrites a client-supplied value with a None from the provider —
    a provider that found nothing for a field must not erase what the CSV
    already had.

    email_previous write rule, stated exactly:

        old_email = row.email  (before this call touches it)
        if result.email and old_email and result.email != old_email:
            row.email_previous = old_email   # a real value is being superseded
        # else: leave email_previous untouched — first-time population
        # (old_email was NULL) and a same-value result both have nothing to
        # preserve.

    Only a *change* to an existing non-null value populates email_previous,
    since there is nothing superseded on first-time population. This is what
    lets stop_active_winback_runs() (src/services/winback_sequencer.py,
    widened in the same change that introduced this column) still match a
    reply against the address a prospect actually replied from, even after a
    later enrichment overwrite.

    requires_enrichment_review rule, stated exactly:

        requires_enrichment_review = (no usable email after enrichment)
                                     AND (no verified phone after enrichment)

    A "usable email" is any non-null email, whatever its email_status —
    deliberately NOT email_status != 'VERIFIED'. Requiring VERIFIED would
    recreate the exact trap contacts.email_status is already in
    (compliance_gate._check_deterministic_columns hard-FAILs on anything
    else, and nothing in this codebase ever writes VERIFIED except
    scripts/e2e_approval_gate.py by hand) — no cold contact in this repo can
    pass its own compliance gate today; this module must not import that bug
    into Win-Back.
    """
    row = session.execute(
        text("SELECT email, phone FROM winback_rows WHERE winback_row_id = :id FOR UPDATE"),
        {"id": winback_row_id},
    ).fetchone()
    if row is None:
        logger.error("owner_enrichment.apply_result: winback_row_id=%s not found", winback_row_id)
        return None

    old_email = row.email
    old_phone = row.phone

    new_email = result.email or old_email
    email_previous = old_email if (result.email and old_email and result.email != old_email) else None

    new_phone = result.phone or old_phone
    newly_discovered_phone: Optional[str] = None
    if result.phone and result.phone != old_phone:
        normalized = _normalize_phone(result.phone)
        if normalized:
            newly_discovered_phone = normalized

    email_status = result.email_status if result.email else "UNVERIFIED"
    has_usable_email = bool(new_email)
    # Tied to result.phone (what THIS call actually verified), not new_phone
    # (which can fall back to a pre-existing, never-verified phone already on
    # the row). EnrichmentResult.__post_init__ already rejects
    # phone_verified=True with phone=None, so this is a second, independent
    # guard against the same mistake, not the only one.
    has_verified_phone = bool(result.phone) and result.phone_verified is True
    requires_review = not has_usable_email and not has_verified_phone

    params = {
        "id": winback_row_id,
        "email": new_email,
        "email_previous": email_previous,
        "email_status": email_status,
        "phone": new_phone,
        "phone_verified": result.phone_verified,
        "requires_review": requires_review,
        "provider": result.provider,
        "now": now,
    }
    set_clauses = [
        "email = :email",
        "email_status = :email_status",
        "phone = :phone",
        "phone_verified = :phone_verified",
        "requires_enrichment_review = :requires_review",
        "enrichment_provider = :provider",
        "enrichment_timestamp = :now",
        "updated_at = :now",
    ]
    if email_previous is not None:
        set_clauses.append("email_previous = :email_previous")
    session.execute(
        text(f"UPDATE winback_rows SET {', '.join(set_clauses)} WHERE winback_row_id = :id"),
        params,
    )
    return ApplyOutcome(
        newly_discovered_phone=newly_discovered_phone,
        requires_enrichment_review=requires_review,
        email=new_email,
        phone=new_phone,
    )


def enrich_homestead_drop_signals(session: Session, provider: OwnerEnrichmentProvider) -> int:
    """Client's "including the new homestead and same-owner rows"
    (blackink-client-comments-04-09-2026.md:28-29). Documented no-op —
    Week2_Tasks_Dev_Split_v1.md:11 explicitly defers both signal types this
    sprint ("Deferred items not in this sprint: … same-owner assessor match,
    homestead signal on Owner Packet"), and no table in this repo produces
    either signal yet: raw_assessor_parcels (migrations/apply_raw_assessor_parcels.py)
    carries no homestead-exemption column as of this writing.

    Called from src/tasks/enrichment_verification.py's sweep so it is on the
    real code path, not orphaned — the DoD accepts a no-op body here
    explicitly ("even if the hook body is a no-op pending that signal's
    arrival"). When the deed engine starts emitting homestead-drop/same-owner
    rows, this body reuses submit()/collect()/apply_result() unchanged — the same
    signal_source tag on the owner_enrichment_completed event ("HOMESTEAD_DROP")
    is what makes "for every signal" auditable per source, no speculative
    dispatch table needed for one live signal type and one no-op."""
    return 0


def build_qa_summary(counts: dict) -> str:
    """Pure function — no I/O — so it is unit-testable without a Slack
    workspace. Returns the #blackink-qa verification-summary text.

    counts keys: enriched, failed, pending, attempts_exhausted,
    submit_failures, provider.

    Two failure modes are deliberately surfaced loudly rather than hidden
    behind healthy-looking counts, since this summary is the pre-pilot green
    light and a human reads it, not just a caller checking an exit code:

      - `provider=stub` means no live verification happened at all (missing
        TRACERFY_API_KEY, or the skip-trace endpoint contract not yet
        wired).
      - `submit_failures > 0` means the vendor call itself is failing (bad
        key, rate limit, or — before the endpoint contract is confirmed —
        TracerfyEnrichmentProvider.submit()'s own NotImplementedError). This
        is worse than provider=stub, because provider reads "tracerfy" (looks
        live) while nothing is actually being verified — without this line,
        the summary would read as a harmless "still pending" rather than a
        broken integration."""
    live_note = "" if counts.get("provider") != "stub" else " — provider=stub, NOT a live verification"
    submit_failures = counts.get("submit_failures", 0)
    failure_note = (
        f" — :warning: {submit_failures} vendor SUBMIT failure(s), see logs — enrichment is NOT running"
        if submit_failures else ""
    )
    return (
        f":mag: Owner enrichment verification — "
        f"enriched={counts.get('enriched', 0)} "
        f"failed={counts.get('failed', 0)} "
        f"pending={counts.get('pending', 0)} "
        f"attempts_exhausted={counts.get('attempts_exhausted', 0)}"
        f"{live_note}{failure_note}"
    )
