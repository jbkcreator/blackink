"""Atomic claim-and-send for the Show-Rate Reminder Cascade (Subtask
3.2.2) — mirrors src/services/calendar_confirmation.py's pattern exactly
(SKIP LOCKED claim, claim-lease recovery, recheck-before-send,
FAILED vs UNCERTAIN), extended with two things confirmation didn't need:

- **`claim_time` is a real parameter, never SQL `NOW()`.** A Python-side
  clock monkeypatch has no effect on Postgres's own clock, so the fast-
  forward timing test (prove a job fires within 60s of its 24h/30min
  mark without a real wall-clock wait) needs the claim comparison itself
  to take an explicit timestamp.
- **BLOCKED and UNCERTAIN are excluded from the claim query.** Unlike a
  definite FAILED (bounded retry) or a fresh PENDING, a BLOCKED job (email
  sending disabled, or Dev 2's Owner Visibility Score/PDF isn't ready
  yet) has a known-unmet precondition that a plain retry won't fix —
  see recover_blocked_jobs() for how it actually un-sticks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.services.calendar_confirmation import UncertainDeliveryError
from src.services.email_dispatch import send_show_rate_24h_reminder, send_show_rate_pre_demo_email

_CLAIM_LEASE_MINUTES = 10
_MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT = 5

# ── Hardened OVS PDF fetch ───────────────────────────────────────────────
# contacts.ovs_pdf_url is written by Dev 2's (not-yet-built) storage step,
# not by this codebase — treat it as untrusted input, not a value we can
# assume is well-formed or safe to fetch blindly.
_OVS_PDF_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB — generous for a 1-2 page report
_OVS_PDF_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_PDF_MAGIC = b"%PDF-"


class UnsafeOvsPdfUrlError(Exception):
    """Raised when contacts.ovs_pdf_url fails a safety check — never
    caught and retried as a generic failure; the reminder job records
    this as its own distinct BLOCKED reason (see send_show_rate_reminder)
    rather than silently attaching whatever bytes came back."""


def claim_reminders(session: Session, *, claim_time: datetime, limit: int = 20) -> List[int]:
    """Atomic SKIP LOCKED claim, identical shape to
    calendar_confirmation.claim_confirmations() — see that module for the
    claim-expiry-recovery rationale. claim_time is always passed
    explicitly by the caller (real datetime.now(timezone.utc) at the real
    call site, or a fixed instant in a fast-forward test) — never read
    from SQL NOW()."""
    rows = session.execute(
        text(
            f"""
            UPDATE booking_reminder_jobs SET status = 'SENDING', attempts = attempts + 1, claimed_at = :claim_time
            WHERE reminder_job_id IN (
                SELECT reminder_job_id FROM booking_reminder_jobs
                WHERE (status = 'PENDING' AND scheduled_for <= :claim_time)
                   OR (status = 'FAILED' AND next_retry_at <= :claim_time)
                   OR (status = 'SENDING' AND claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
                ORDER BY reminder_job_id FOR UPDATE SKIP LOCKED LIMIT :limit
            )
            RETURNING reminder_job_id
            """
        ),
        {"claim_time": claim_time, "limit": limit},
    ).fetchall()
    return [r.reminder_job_id for r in rows]


def recover_blocked_jobs(session: Session, *, email_sending_enabled: bool) -> None:
    """Self-heal step run before every claim pass — cheap because it's
    scoped to BLOCKED rows only, not the whole table.

    - EMAIL_SENDING_DISABLED rows recover the instant the config flag
      flips True — checked by the caller against the real settings
      object, not stored/queried here.
    - MISSING_OVS_SCORE/MISSING_OVS_PDF rows re-check the underlying data
      directly (does a score row exist now? does contacts.ovs_pdf_url
      resolve now?) rather than waiting on an event stream — the moment
      Dev 2's pipeline actually populates either, the very next sweep
      tick un-blocks the job automatically, including a manually-seeded
      ovs_pdf_url used for integration testing."""
    if email_sending_enabled:
        session.execute(
            text(
                "UPDATE booking_reminder_jobs SET status = 'PENDING', updated_at = NOW() "
                "WHERE status = 'BLOCKED' AND last_error = 'EMAIL_SENDING_DISABLED'"
            )
        )
    session.execute(
        text(
            """
            UPDATE booking_reminder_jobs brj SET status = 'PENDING', updated_at = NOW()
            FROM bookings b
            WHERE brj.status = 'BLOCKED' AND brj.last_error = 'MISSING_OVS_SCORE'
              AND brj.booking_id = b.booking_id
              AND EXISTS (SELECT 1 FROM owner_visibility_scores ovs WHERE ovs.company_id = b.target_company_id)
            """
        )
    )
    session.execute(
        text(
            """
            UPDATE booking_reminder_jobs brj SET status = 'PENDING', updated_at = NOW()
            FROM bookings b JOIN contacts c ON c.contact_id = b.target_contact_id
            WHERE brj.status = 'BLOCKED' AND brj.last_error = 'MISSING_OVS_PDF'
              AND brj.booking_id = b.booking_id
              AND c.ovs_pdf_url IS NOT NULL
            """
        )
    )


def _load_job(session: Session, reminder_job_id: int):
    return session.execute(
        text(
            "SELECT rj.reminder_job_id, rj.booking_id, rj.reminder_step, rj.status AS job_status, rj.attempts, "
            "b.client_id, b.event_status, b.scheduled_at, b.raw_payload, b.target_company_id, b.target_contact_id, "
            "c.email AS target_email, c.first_name AS target_first_name, c.phone AS target_phone, "
            "cty.county_name "
            "FROM booking_reminder_jobs rj "
            "JOIN bookings b ON rj.booking_id = b.booking_id "
            "LEFT JOIN contacts c ON b.target_contact_id = c.contact_id "
            "LEFT JOIN companies co ON b.target_company_id = co.company_id "
            "LEFT JOIN counties cty ON co.county_slug = cty.county_slug "
            "WHERE rj.reminder_job_id = :id"
        ),
        {"id": reminder_job_id},
    ).one()


def _mark(session: Session, reminder_job_id: int, status: str, *, error: Optional[str] = None, next_retry_at=None) -> None:
    session.execute(
        text(
            "UPDATE booking_reminder_jobs SET status = :status, last_error = :error, "
            "next_retry_at = :next_retry_at, updated_at = NOW() WHERE reminder_job_id = :id"
        ),
        {"status": status, "error": error, "next_retry_at": next_retry_at, "id": reminder_job_id},
    )


def _resolve_timezone_label(job) -> Optional[str]:
    """Priority order, each a real signal, no invented fallback:
    1. Provider-stated timezone from the booking's own raw_payload
       (Google/Microsoft events carry start.timeZone; GHL payloads carry
       none).
    2. Phone area-code fallback via us_area_code_timezones, reusing the
       exact lookup already in campaign_readiness_gate.py's
       _in_quiet_hours (phone[2:5] -> iana_timezone).
    3. Explicit unresolved state (None) — Florida alone spans Eastern and
       Central, so no single state-level default is safe to assume."""
    import json as _json

    raw_payload = job.raw_payload
    if isinstance(raw_payload, str):
        raw_payload = _json.loads(raw_payload)
    provider_tz = ((raw_payload or {}).get("start") or {}).get("timeZone")
    if provider_tz:
        return provider_tz
    return None  # phone-based fallback needs a session — resolved by the caller (session-level lookup below)


def _resolve_timezone_via_phone(session: Session, phone: Optional[str]) -> Optional[str]:
    if not phone or not phone.startswith("+1") or len(phone) < 5:
        return None
    area_code = phone[2:5]
    row = session.execute(
        text("SELECT iana_timezone FROM us_area_code_timezones WHERE area_code = :area_code"),
        {"area_code": area_code},
    ).first()
    return row.iana_timezone if row else None


def _resolve_view_event_link(job) -> Optional[str]:
    """Google Calendar's htmlLink / Microsoft Graph's webLink are
    event-VIEW links ("open this event in Calendar"), not reschedule
    actions — labelled honestly as "View calendar event", never as a
    reschedule link. No real provider reschedule URL/action exists
    anywhere in this repo's provider contracts, so this plan does not
    fabricate one (no mailto:, no relabeled event-view link)."""
    import json as _json

    raw_payload = job.raw_payload
    if isinstance(raw_payload, str):
        raw_payload = _json.loads(raw_payload)
    return (raw_payload or {}).get("htmlLink") or (raw_payload or {}).get("webLink")


def _fetch_ovs_pdf(url: str) -> bytes:
    """Fetches contacts.ovs_pdf_url with the checks a URL from an
    untrusted, not-yet-built upstream (Dev 2's storage step) demands:

    - approved host allowlist (settings.ovs_pdf_allowed_hosts) — fails
      closed if unconfigured, exactly like email_sending_enabled
    - https only
    - redirects never followed (a redirect to an unapproved host would
      otherwise bypass the allowlist check entirely)
    - a real connect/read timeout, not an indefinite hang
    - a hard byte cap enforced while streaming, not just Content-Length
      (a malicious or misconfigured host could omit or lie about it)
    - Content-Type and the actual leading bytes (%PDF- magic) both
      checked — a wrong Content-Type alone isn't trusted, and neither is
      an unverified body just because the header looked right
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeOvsPdfUrlError(f"ovs_pdf_url is not https: {parsed.scheme!r}")
    allowed_hosts = get_settings().ovs_pdf_allowed_hosts
    if not allowed_hosts:
        raise UnsafeOvsPdfUrlError("OVS_PDF_ALLOWED_HOSTS is not configured — no host is approved")
    if (parsed.hostname or "").lower() not in allowed_hosts:
        raise UnsafeOvsPdfUrlError(f"ovs_pdf_url host {parsed.hostname!r} is not in the approved allowlist")

    with httpx.stream(
        "GET", url, timeout=_OVS_PDF_TIMEOUT, follow_redirects=False,
    ) as resp:
        if resp.status_code != 200:
            raise UnsafeOvsPdfUrlError(f"ovs_pdf_url returned HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "")
        if "application/pdf" not in content_type.lower():
            raise UnsafeOvsPdfUrlError(f"ovs_pdf_url Content-Type is not application/pdf: {content_type!r}")

        chunks = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > _OVS_PDF_MAX_BYTES:
                raise UnsafeOvsPdfUrlError(f"ovs_pdf_url body exceeds {_OVS_PDF_MAX_BYTES} bytes — aborted mid-stream")
            chunks.append(chunk)
        body = b"".join(chunks)

    if not body.startswith(_PDF_MAGIC):
        raise UnsafeOvsPdfUrlError("ovs_pdf_url body does not start with the PDF magic bytes (%PDF-)")
    return body


def send_show_rate_reminder(session: Session, reminder_job_id: int, *, as_of: datetime) -> None:
    """Called only on a job already claimed (status='SENDING') by
    claim_reminders(). Rechecks the booking's live state immediately
    before sending — not just at claim time — since a genuinely
    concurrent cancellation, or the meeting's start time itself having
    already passed, can land in between."""
    job = _load_job(session, reminder_job_id)

    if job.event_status == "CANCELLED":
        _mark(session, reminder_job_id, "CANCELLED")
        return
    if job.scheduled_at is not None and as_of >= job.scheduled_at:
        # The meeting already started (or is starting right now) — a
        # reminder for a meeting that's already underway/over is stale,
        # never sent, regardless of why the job is only being processed
        # this late (a long BLOCKED spell, a worker outage, etc.).
        _mark(session, reminder_job_id, "SKIPPED", error="meeting already started before send")
        return
    if job.target_email is None:
        _mark(session, reminder_job_id, "FAILED", error="no target contact email on file")
        return

    tz_label = _resolve_timezone_label(job) or _resolve_timezone_via_phone(session, job.target_phone)

    try:
        if job.reminder_step == "24h_email":
            if job.target_company_id is None:
                _mark(session, reminder_job_id, "BLOCKED", error="MISSING_OVS_SCORE")
                return
            score_row = session.execute(
                text(
                    "SELECT county_rank, county_percentile FROM owner_visibility_scores "
                    "WHERE company_id = :cid ORDER BY month_key DESC LIMIT 1"
                ),
                {"cid": job.target_company_id},
            ).first()
            if score_row is None:
                _mark(session, reminder_job_id, "BLOCKED", error="MISSING_OVS_SCORE")
                return
            message_id = send_show_rate_24h_reminder(
                session,
                client_id=job.client_id,
                target_email=job.target_email,
                target_name=job.target_first_name,
                scheduled_at=job.scheduled_at,
                county_name=job.county_name or "your county",
                county_rank=score_row.county_rank,
                county_percentile=score_row.county_percentile,
                local_time_label=tz_label,
                view_event_link=_resolve_view_event_link(job),
            )
        elif job.reminder_step == "30min_email":
            pdf_row = session.execute(
                text("SELECT ovs_pdf_url FROM contacts WHERE contact_id = :id"),
                {"id": job.target_contact_id},
            ).first()
            if pdf_row is None or pdf_row.ovs_pdf_url is None:
                _mark(session, reminder_job_id, "BLOCKED", error="MISSING_OVS_PDF")
                return
            try:
                pdf_bytes = _fetch_ovs_pdf(pdf_row.ovs_pdf_url)
            except UnsafeOvsPdfUrlError as exc:
                # Distinct BLOCKED reason from MISSING_OVS_PDF — the URL
                # exists but failed a safety check (unapproved host, wrong
                # content, oversized, etc.), a data-quality problem in
                # Dev 2's pipeline, not something a plain retry fixes and
                # not something recover_blocked_jobs() auto-heals (unlike
                # MISSING_OVS_PDF, the row existing again next sweep tick
                # doesn't mean this got fixed).
                _mark(session, reminder_job_id, "BLOCKED", error=f"UNSAFE_OVS_PDF_URL: {exc}")
                return
            message_id = send_show_rate_pre_demo_email(
                session,
                client_id=job.client_id,
                target_email=job.target_email,
                target_name=job.target_first_name,
                scheduled_at=job.scheduled_at,
                local_time_label=tz_label,
                ovs_pdf_bytes=pdf_bytes,
            )
        else:
            raise ValueError(f"Unknown reminder_step: {job.reminder_step}")
    except UncertainDeliveryError as exc:
        _mark(session, reminder_job_id, "UNCERTAIN", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - any other provider failure is a definite, retryable failure
        if job.attempts >= _MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT:
            _mark(session, reminder_job_id, "FAILED", error=str(exc))
        else:
            backoff_minutes = 2 ** job.attempts
            _mark(
                session, reminder_job_id, "FAILED", error=str(exc),
                next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes),
            )
        return

    if message_id is None:
        # send_show_rate_*'s own settings.email_sending_enabled check
        # already returns None (not an exception) for the disabled case —
        # BLOCKED, not PENDING/FAILED, so the claim query never
        # re-reclaims it every tick (see recover_blocked_jobs()).
        _mark(session, reminder_job_id, "BLOCKED", error="EMAIL_SENDING_DISABLED")
        return

    _mark(session, reminder_job_id, "SENT")
    session.execute(
        text(
            "INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
            "VALUES (:client_id, 'show_rate_reminder_sent', 'booking_reminder_job', :entity_id, "
            "jsonb_build_object('reminder_step', :step))"
        ),
        {"client_id": job.client_id, "entity_id": str(reminder_job_id), "step": job.reminder_step},
    )
