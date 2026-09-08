"""Unit tests for src/services/tracerfy_client.py's scrub_phones() —
specifically the PR-review finding that a formatted phone in Tracerfy's
RETURNED CSV (e.g. "+1 (813) 555-0100") must be normalized the same way as
the submitted phone before being used as a result-dict key, or the caller's
exact-match lookup silently misses it. submit_batch/poll_queue mocked out
— no network call.
"""

from unittest.mock import patch

from src.services.tracerfy_client import scrub_phones


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
