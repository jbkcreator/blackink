"""Tracerfy DNC-scrub HTTP client — the one place this vendor's API is
called from.

Extracted out of src/tasks/dnc_refresh.py (Subtask 3.1.1) so
dnc_refresh.py's monthly batch and winback_ingest.py's own post-disposition
batch scrub share exactly one implementation of the vendor's submit-batch /
poll-queue / download-CSV flow, per reuse_ledger_week0.md's own stated
intent ("Shares DncProvider / EmailVerificationProvider interfaces with
compliance_gate.py — no duplicate vendor code"). dnc_refresh.py imports the
primitives from here instead of defining its own copies. Deliberately NOT
wired into compliance_gate.py's live per-contact gate — see that module's
own comment on why (Tracerfy's queue is a batch-shaped API, unsuited to a
synchronous per-contact check).

API shape (Tracerfy /v1/api/):
  POST /dnc/scrub/  {"phones": [...]}  -> {"dnc_queue_id": "..."}
  GET  /dnc/queue/{id}                 -> {"pending": bool, "download_url": "..."}
  CSV columns: phone, national_dnc (Y/N), litigator (Y/N)

Cost: 1 Tracerfy credit (~$0.02) per phone checked.

Skip-trace (Subtask 3.2.1 — owner name/address -> phone/email) is the SAME
Tracerfy account (client decision, 2026-09-08 — one vendor for both DNC and
skip-trace) but a DIFFERENT product with its own, now-confirmed contract —
see the "Skip-trace" section below for the full shape and sourcing.
"""

import csv
import io
import json
import logging
import time

import requests

from src.services.email_suppression import _normalize_phone

logger = logging.getLogger(__name__)

_TRACERFY_BASE = "https://tracerfy.com/v1/api"
_SCRUB_ENDPOINT = f"{_TRACERFY_BASE}/dnc/scrub/"
_QUEUE_ENDPOINT = f"{_TRACERFY_BASE}/dnc/queue/"
_POLL_INTERVAL_SEC = 5
_POLL_MAX_ATTEMPTS = 120  # 10 minutes
BATCH_SIZE = 1000  # exported — dnc_refresh.py chunks its monthly sweep by this


def headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def submit_batch(phones: list[str], api_key: str) -> str:
    resp = requests.post(
        _SCRUB_ENDPOINT,
        headers=headers(api_key),
        json={"phones": phones},
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Tracerfy DNC: invalid API key ({resp.status_code})")
    if resp.status_code == 429:
        raise RuntimeError("Tracerfy DNC: rate limited (429)")
    if not resp.ok:
        raise RuntimeError(f"Tracerfy DNC HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    queue_id = data.get("dnc_queue_id") or data.get("queue_id") or data.get("id")
    if not queue_id:
        raise RuntimeError(f"Tracerfy DNC: no queue_id in response: {data}")
    return str(queue_id)


def poll_queue(queue_id: str, api_key: str) -> list[dict]:
    url = f"{_QUEUE_ENDPOINT}{queue_id}"
    for attempt in range(_POLL_MAX_ATTEMPTS):
        resp = requests.get(url, headers=headers(api_key), timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Tracerfy queue poll HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not data.get("pending", True):
            download_url = data.get("download_url", "")
            if not download_url:
                logger.warning("tracerfy_client: queue=%s complete but no download_url", queue_id)
                return []
            csv_resp = requests.get(download_url, timeout=60)
            if not csv_resp.ok:
                raise RuntimeError(f"Tracerfy CSV download HTTP {csv_resp.status_code}")
            return list(csv.DictReader(io.StringIO(csv_resp.text)))
        logger.info("tracerfy_client: queue=%s pending attempt=%d", queue_id, attempt + 1)
        time.sleep(_POLL_INTERVAL_SEC)
    raise RuntimeError(f"Tracerfy queue {queue_id} did not complete within 10 minutes")


def is_dnc_hit(row: dict) -> bool:
    """True if a Tracerfy result-CSV row indicates the phone is DNC-listed
    or a known litigator — the exact 'Y'/'YES'/'TRUE'/'1' parsing dnc_refresh
    used inline before this was extracted."""
    national_dnc = str(row.get("national_dnc", "")).strip().upper() in ("Y", "YES", "TRUE", "1")
    litigator = str(row.get("litigator", "")).strip().upper() in ("Y", "YES", "TRUE", "1")
    return national_dnc or litigator


def scrub_phones(phones: list[str], api_key: str) -> dict[str, bool]:
    """Single submit+poll round trip for up to BATCH_SIZE phones (no
    internal chunking — callers with more than BATCH_SIZE phones chunk
    their own loop, same as dnc_refresh.py's monthly sweep does).

    `phones` must already be normalized (callers submit digits-only,
    country-code-stripped values — see _normalize_phone). The RETURNED CSV's
    phone column is normalized the same way before being used as a result
    key: Tracerfy is free to echo the phone back in a different shape
    (formatted, with a country code, etc.) than what was submitted, and an
    exact-string match against the normalized submission would silently
    miss it — the same normalize-both-sides discipline dnc_refresh.py's own
    _persist_results already applies. A phone missing from the return value
    means Tracerfy's result CSV didn't include it at all — callers must
    treat that as unknown, never assume clean.

    Raises on a total submit/poll failure (network error, bad key, timeout)
    — callers decide how to treat "the vendor call itself failed" (e.g. the
    compliance gate's ABSTAIN-on-unknown posture), this function never
    silently returns an empty/clean result for that case.
    """
    if len(phones) > BATCH_SIZE:
        raise ValueError(f"scrub_phones: {len(phones)} phones exceeds BATCH_SIZE={BATCH_SIZE} — chunk the caller's loop")
    queue_id = submit_batch(phones, api_key)
    csv_rows = poll_queue(queue_id, api_key)
    result: dict[str, bool] = {}
    for row in csv_rows:
        phone = _normalize_phone(str(row.get("phone", "")).strip())
        if not phone:
            continue
        result[phone] = not is_dnc_hit(row)
    return result


# ============================================================================
# Skip-trace (Subtask 3.2.1) — owner name/address -> phone/email
# ============================================================================
#
# CONFIRMED CONTRACT (2026-09-08) — cross-checked against Tracerfy's own API
# docs (tracerfy.com/skip-tracing-api-documentation) AND the working,
# production ForcedAction-System integration
# (ForcedAction-System/Forced-action-/src/services/tracerfy_batch.py — same
# vendor, same account type, already live). Both agree.
#
# This is a DIFFERENT product from the DNC scrub above, with a different
# request format (multipart/form-data, not JSON), a different submit
# response key (queue_id, not dnc_queue_id), and a fundamentally different
# poll contract (see poll_skiptrace_queue — it does NOT return
# {"pending": bool, "download_url": ...} the way poll_queue() does for DNC;
# it returns the result rows directly, and completion has to be detected by
# a stability window because results stream in). Reusing poll_queue() for
# skip-trace would silently misparse a real response — do not do that.
#
# Endpoint:   POST https://tracerfy.com/v1/api/trace/     (submit, multipart)
#             GET  https://tracerfy.com/v1/api/queue/{id} (poll, returns a
#                  JSON array directly — [] while still processing)
# Auth:       Authorization: Bearer <key>  (same as DNC)
# Cost:       trace_type="normal" (name+address) = 1 credit (~$0.02)/hit,
#             0 on a miss. This repo only ever uses "normal" — Win-Back rows
#             always carry owner_name (a required CSV column), so the
#             cheaper name+address tier applies; the address-only "advanced"
#             tier ($0.04/hit) that ForcedAction falls back to for
#             corporate/untraceable names is not needed here.
# Rate limit: ~10 POST /trace/ per 5-minute window per ForcedAction's own
#             production experience (not stated in Tracerfy's docs) — not
#             enforced by this module; a client with a backlog large enough
#             to need multiple _TRACE_BATCH_SIZE-sized submissions per sweep
#             run would need a delay between them. Not added here since
#             OWNER_ENRICHMENT_MAX_PER_RUN (default 500) keeps a single
#             sweep well under one batch's worth at _TRACE_BATCH_SIZE=1000.
#
# Two gaps Tracerfy's batch API creates that this repo's schema doesn't
# natively cover, both handled in owner_enrichment.TracerfyEnrichmentProvider,
# not here (this module stays pure HTTP transport):
#   1. No submitted-row ID is echoed back in the result — ForcedAction's own
#      code comment confirms this explicitly ("the queue endpoint does not
#      echo our label/owner_id"). Matching is by normalized street address.
#   2. city/state are separate required request fields, but winback_rows
#      only stores one freeform address string — see
#      owner_enrichment.py's address-splitting helper.

_TRACE_ENDPOINT = f"{_TRACERFY_BASE}/trace/"
_TRACE_QUEUE_ENDPOINT = f"{_TRACERFY_BASE}/queue/"
# No documented API cap on batch size; kept well under ForcedAction's own
# 3000 as an untested-volume-appropriate default for this codebase's first
# skip-trace integration — raise once real volume is observed.
_TRACE_BATCH_SIZE = 1000
_TRACE_POLL_INTERVAL_SEC = 5
_TRACE_MAX_POLL_ATTEMPTS = 120  # 10 minutes at 5s/poll, matching poll_queue()'s own DNC budget
# Results stream in rather than arriving all at once — accept only once the
# row count has held steady for this many consecutive polls AND this many
# seconds have elapsed since the first non-empty response. Directly ported
# from ForcedAction's own tracerfy_batch.py, whose comment records the
# failure mode this protects against: "the failure mode that previously
# stranded billed-but-uningested hits" from accepting an early plateau.
_TRACE_STABLE_ROUNDS_REQUIRED = 2
_TRACE_MIN_SETTLE_SECONDS = 30


def submit_skiptrace_batch(records: list[dict], api_key: str) -> tuple[str, int]:
    """POST /trace/ as multipart/form-data (NOT JSON — confirmed contract,
    see module docstring). Returns (queue_id, estimated_wait_seconds).

    `records`: list of {"label": str, "first_name": str, "last_name": str,
    "address": str, "city": str, "state": str} — already split/parsed by
    the caller (owner_enrichment.TracerfyEnrichmentProvider); this function
    does no address or name parsing, pure transport only. `label` is sent
    for traceability in Tracerfy's own dashboard only — it is NOT echoed
    back in the queue result (confirmed), so callers must not rely on it
    for matching.

    Raises RuntimeError on a submit-stage failure (bad key, rate limit,
    non-2xx) — same posture as submit_batch() for DNC. Nothing is queued or
    billed if this raises; see OwnerEnrichmentProvider's own docstring for
    why that distinction matters to the caller's retry-attempt accounting.
    """
    if len(records) > _TRACE_BATCH_SIZE:
        raise ValueError(f"submit_skiptrace_batch: {len(records)} records exceeds _TRACE_BATCH_SIZE={_TRACE_BATCH_SIZE} — chunk the caller's loop")
    fields = {
        "address_column": "address",
        "city_column": "city",
        "state_column": "state",
        "first_name_column": "first_name",
        "last_name_column": "last_name",
        "trace_type": "normal",
        "json_data": json.dumps(records),
    }
    multipart = {k: (None, v) for k, v in fields.items()}
    resp = requests.post(
        _TRACE_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}"},  # no Content-Type — requests sets the multipart boundary itself
        files=multipart,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Tracerfy skip-trace: invalid API key ({resp.status_code})")
    if resp.status_code == 429:
        raise RuntimeError("Tracerfy skip-trace: rate limited (429)")
    if not resp.ok:
        raise RuntimeError(f"Tracerfy skip-trace HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    queue_id = data.get("queue_id") or data.get("id")
    if not queue_id:
        raise RuntimeError(f"Tracerfy skip-trace: no queue_id in response: {data}")
    estimated_wait = int(data.get("estimated_wait_seconds") or 30)
    return str(queue_id), estimated_wait


def poll_skiptrace_queue(queue_id: str, api_key: str, estimated_wait_seconds: int = 30) -> list[dict]:
    """GET /queue/{id} until results stabilize. Confirmed response shape is
    the result array DIRECTLY (not DNC's {"pending": bool, "download_url"}
    wrapper) — an empty list means still processing, a non-empty list may
    still be partial (Tracerfy streams results in), so completion requires
    the row count to hold steady across _TRACE_STABLE_ROUNDS_REQUIRED polls
    AND _TRACE_MIN_SETTLE_SECONDS to have elapsed since the first non-empty
    response. Ported from ForcedAction's own proven implementation.

    Each result row's confirmed fields: address, city, state, mail_address,
    mail_city, mail_state, first_name, last_name, primary_phone,
    primary_phone_type ("Mobile"/"Landline"), mobile_1..mobile_5,
    landline_1..landline_3, email_1..email_5. No verification/confidence
    field of any kind — a phone or email present in the response IS the
    hit; there is nothing to check it against.

    Raises RuntimeError on an HTTP failure during polling — by this point
    the batch has already been submitted (and may be billing), so the
    caller must treat this as a spent attempt, never a free retry. Returns
    [] (not an error) if the queue never produces a stable result within
    the poll budget — every submitted row is then absent from the caller's
    match, which src/services/owner_enrichment.py already treats as
    not-found, never as clean.
    """
    url = f"{_TRACE_QUEUE_ENDPOINT}{queue_id}"
    time.sleep(estimated_wait_seconds)

    last_count = -1
    stable_rounds = 0
    first_nonempty_at = None

    for attempt in range(_TRACE_MAX_POLL_ATTEMPTS):
        resp = requests.get(url, headers=headers(api_key), timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Tracerfy skip-trace queue poll HTTP {resp.status_code}: {resp.text[:300]}")
        results = resp.json()
        current_count = len(results)

        if current_count == 0:
            logger.info("tracerfy_client: skiptrace queue=%s still empty attempt=%d", queue_id, attempt + 1)
        else:
            if first_nonempty_at is None:
                first_nonempty_at = time.monotonic()
            if current_count == last_count:
                stable_rounds += 1
                settled = (time.monotonic() - first_nonempty_at) >= _TRACE_MIN_SETTLE_SECONDS
                if stable_rounds >= _TRACE_STABLE_ROUNDS_REQUIRED and settled:
                    logger.info(
                        "tracerfy_client: skiptrace queue=%s complete: %d rows stable, settled",
                        queue_id, current_count,
                    )
                    return results
            else:
                stable_rounds = 0
                logger.info("tracerfy_client: skiptrace queue=%s growing: %d rows attempt=%d", queue_id, current_count, attempt + 1)

        last_count = current_count
        time.sleep(_TRACE_POLL_INTERVAL_SEC)

    logger.warning("tracerfy_client: skiptrace queue=%s did not stabilise within poll budget", queue_id)
    return []


# NOTE: deliberately no combined submit+poll "skiptrace_batch()" convenience
# function here (unlike scrub_phones() for DNC) — owner_enrichment.py's
# TracerfyEnrichmentProvider needs submit_skiptrace_batch() and
# poll_skiptrace_queue() as two SEPARATE calls with a commit in between (the
# enrichment_attempts bump happens between them), for the crash-safety
# reasons documented on OwnerEnrichmentProvider's submit()/collect() split. A
# combined function would hide that boundary and invite exactly the bug this
# design avoids.
