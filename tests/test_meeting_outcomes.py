from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.services.meeting_outcomes import record_outcome


def _fake_session():
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    return session


def test_record_outcome_upserts_and_mirrors_to_company_and_contact():
    session = _fake_session()
    with patch("src.services.meeting_outcomes.get_db_context") as mock_ctx, \
         patch("src.services.meeting_outcomes.log_event") as mock_log:
        mock_ctx.return_value = session
        record_outcome(
            "acme_pm",
            contact_id=42,
            meeting_occurred_at=datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc),
            attendance_status="Held",
            pm_software="AppFolio",
            door_count_est=120,
            objections=["Pricing", "Capacity"],
            next_action="Send proposal Friday",
            recorded_by="slack:U123",
        )
    executed = [str(c.args[0]) for c in session.execute.call_args_list if c.args]
    assert any("INSERT INTO meeting_outcomes" in s and "ON CONFLICT" in s for s in executed)
    assert any("UPDATE contacts" in s and "prospect_objections" in s for s in executed)
    mock_log.assert_called_once()
    assert mock_log.call_args.args[1] == "meeting_outcome_recorded"


def test_record_outcome_rejects_invalid_attendance_status():
    with pytest.raises(ValueError):
        record_outcome(
            "acme_pm", contact_id=1, meeting_occurred_at=datetime.now(timezone.utc),
            attendance_status="Ghosted", pm_software=None, door_count_est=None,
            objections=[], next_action="", recorded_by="slack:U1",
        )
