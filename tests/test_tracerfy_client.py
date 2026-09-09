"""Unit tests for src/services/tracerfy_client.py's scrub_phones() —
specifically the PR-review finding that a formatted phone in Tracerfy's
RETURNED CSV (e.g. "+1 (813) 555-0100") must be normalized the same way as
the submitted phone before being used as a result-dict key, or the caller's
exact-match lookup silently misses it. submit_batch/poll_queue mocked out
— no network call.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.services.tracerfy_client import poll_skiptrace_queue, scrub_phones, submit_skiptrace_batch


def _mock_tracerfy(csv_rows):
	return patch.multiple(
		"src.services.tracerfy_client",
		submit_batch=lambda phones, api_key: "queue-1",
		poll_queue=lambda queue_id, api_key: csv_rows,
	)


def test_scrub_phones_normalizes_formatted_returned_phone():
	# Submitted normalized ("8135550100"); Tracerfy echoes it back formatted.
	with _mock_tracerfy([{"phone": "+1 (813) 555-0100", "national_dnc": "N", "litigator": "N"}]):
		result = scrub_phones(["8135550100"], "fake-key")
	assert result == {"8135550100": True}


def test_scrub_phones_normalizes_country_code_prefixed_returned_phone():
	with _mock_tracerfy([{"phone": "18135550100", "national_dnc": "N", "litigator": "N"}]):
		result = scrub_phones(["8135550100"], "fake-key")
	assert result == {"8135550100": True}


def test_scrub_phones_dnc_listed_result_matches_after_normalization():
	with _mock_tracerfy([{"phone": "+1-813-555-0100", "national_dnc": "Y", "litigator": "N"}]):
		result = scrub_phones(["8135550100"], "fake-key")
	assert result == {"8135550100": False}


def test_scrub_phones_multiple_formatted_results():
	with _mock_tracerfy(
		[
			{"phone": "(813) 555-0100", "national_dnc": "N", "litigator": "N"},
			{"phone": "1 727 555 0199", "national_dnc": "Y", "litigator": "N"},
		]
	):
		result = scrub_phones(["8135550100", "7275550199"], "fake-key")
	assert result == {"8135550100": True, "7275550199": False}


def test_scrub_phones_missing_result_stays_absent():
	# A phone Tracerfy simply didn't return a row for must not silently
	# appear as clean — absence from the dict is the caller's "unknown" signal.
	with _mock_tracerfy([{"phone": "8135550100", "national_dnc": "N", "litigator": "N"}]):
		result = scrub_phones(["8135550100", "7275550199"], "fake-key")
	assert result == {"8135550100": True}
	assert "7275550199" not in result


# ---------------------------------------------------------------------------
# submit_skiptrace_batch / poll_skiptrace_queue (Subtask 3.2.1) — confirmed
# contract cross-checked against Tracerfy's own docs and the working
# ForcedAction-System reference integration. Different from DNC in three
# ways this file's tests exercise: multipart (not JSON) request, a plain
# `queue_id` response key (not `dnc_queue_id`), and a poll response that is
# the result array directly (not a {"pending","download_url"} wrapper).
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None, ok=None):
	resp = MagicMock()
	resp.status_code = status_code
	resp.ok = ok if ok is not None else (200 <= status_code < 300)
	resp.json.return_value = json_data or {}
	resp.text = str(json_data)
	return resp


def test_submit_skiptrace_batch_posts_multipart_with_correct_field_names():
	records = [{"label": "1", "first_name": "Jane", "last_name": "Doe", "address": "123 Main St", "city": "Tampa", "state": "FL"}]
	with patch("src.services.tracerfy_client.requests.post") as mock_post:
		mock_post.return_value = _mock_response(json_data={"queue_id": 456, "estimated_wait_seconds": 30})
		queue_id, wait = submit_skiptrace_batch(records, "fake-key")
	assert queue_id == "456"
	assert wait == 30
	call = mock_post.call_args
	assert call.args[0] == "https://tracerfy.com/v1/api/trace/"
	# multipart via `files=`, not `json=` — the DNC endpoint uses json=, skip-trace does not
	assert "json" not in call.kwargs
	fields = call.kwargs["files"]
	assert fields["address_column"] == (None, "address")
	assert fields["city_column"] == (None, "city")
	assert fields["state_column"] == (None, "state")
	assert fields["trace_type"] == (None, "normal")
	assert "Content-Type" not in call.kwargs["headers"]  # requests sets the multipart boundary itself
	assert call.kwargs["headers"]["Authorization"] == "Bearer fake-key"


def test_submit_skiptrace_batch_raises_on_bad_key():
	with patch("src.services.tracerfy_client.requests.post") as mock_post:
		mock_post.return_value = _mock_response(status_code=401, ok=False)
		with pytest.raises(RuntimeError, match="invalid API key"):
			submit_skiptrace_batch([{"label": "1"}], "bad-key")


def test_submit_skiptrace_batch_raises_when_no_queue_id_in_response():
	with patch("src.services.tracerfy_client.requests.post") as mock_post:
		mock_post.return_value = _mock_response(json_data={"message": "ok, no id"})
		with pytest.raises(RuntimeError, match="no queue_id"):
			submit_skiptrace_batch([{"label": "1"}], "fake-key")


def test_submit_skiptrace_batch_rejects_oversized_batch():
	from src.services.tracerfy_client import _TRACE_BATCH_SIZE
	with pytest.raises(ValueError):
		submit_skiptrace_batch([{"label": str(i)} for i in range(_TRACE_BATCH_SIZE + 1)], "fake-key")


def test_poll_skiptrace_queue_returns_result_array_directly_once_stable():
	# Confirmed shape: GET /queue/{id} returns the result list directly —
	# no {"pending": bool, "download_url": ...} wrapper like DNC's poll_queue.
	# Needs 3 identical-count responses to actually return: the 1st sets the
	# "first seen" baseline (count vs. the -1 sentinel always looks like
	# growth), the 2nd is stable-round 1, the 3rd is stable-round 2 (the
	# _TRACE_STABLE_ROUNDS_REQUIRED threshold) with enough elapsed time to
	# also satisfy _TRACE_MIN_SETTLE_SECONDS.
	rows = [{"address": "123 Main St", "email_1": "jane@example.com"}]
	responses = [MagicMock(ok=True, json=lambda: rows) for _ in range(3)]
	with patch("src.services.tracerfy_client.requests.get", side_effect=responses), \
		patch("src.services.tracerfy_client.time.sleep"), \
		patch("src.services.tracerfy_client.time.monotonic", side_effect=[0.0, 10.0, 35.0]):
		result = poll_skiptrace_queue("456", "fake-key", estimated_wait_seconds=0)
	assert result == rows


def test_poll_skiptrace_queue_growing_count_resets_stability_and_does_not_return_early():
	# Results stream in — an early plateau at a low count must NOT be
	# accepted (the exact failure mode ForcedAction's own comments record
	# having been bitten by: "billed-but-uningested hits").
	partial = [{"address": "123 Main St"}]
	full = [{"address": "123 Main St"}, {"address": "456 Oak Ave"}]
	responses = [
		MagicMock(ok=True, json=lambda: partial),  # count=1, first non-empty (monotonic call #1: sets first_nonempty_at)
		MagicMock(ok=True, json=lambda: full),      # count=2, growing vs. last_count=1 -> resets stability, no monotonic call
		MagicMock(ok=True, json=lambda: full),      # count=2, stable round 1 (monotonic call #2: settled check, not enough rounds yet)
		MagicMock(ok=True, json=lambda: full),      # count=2, stable round 2 + settled (monotonic call #3) -> return
	]
	with patch("src.services.tracerfy_client.requests.get", side_effect=responses), \
		patch("src.services.tracerfy_client.time.sleep"), \
		patch("src.services.tracerfy_client.time.monotonic", side_effect=[0.0, 10.0, 40.0]):
		result = poll_skiptrace_queue("456", "fake-key", estimated_wait_seconds=0)
	assert result == full


def test_poll_skiptrace_queue_raises_on_http_failure():
	with patch("src.services.tracerfy_client.requests.get") as mock_get, \
		patch("src.services.tracerfy_client.time.sleep"):
		mock_get.return_value = _mock_response(status_code=500, ok=False)
		with pytest.raises(RuntimeError, match="queue poll HTTP"):
			poll_skiptrace_queue("456", "fake-key", estimated_wait_seconds=0)
