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
"""

import csv
import io
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
