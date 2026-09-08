"""Tests for winback_content.render_winback_touch — merge-tag template
copy for the 3-touch win-back sequence (Subtask 3.1.2)."""

import pytest

from src.services.winback_content import WINBACK_TOUCH_STEPS, render_winback_touch


def test_invalid_touch_step_raises():
	with pytest.raises(ValueError):
		render_winback_touch(4, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St")


def test_all_touch_steps_are_renderable():
	for step in WINBACK_TOUCH_STEPS:
		subject, body, version = render_winback_touch(
			step, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St",
		)
		assert subject and body and version
		assert "SMS" not in body and "text message" not in body.lower()


def test_touch1_uses_generic_hook_when_loss_estimate_missing():
	_, body, _ = render_winback_touch(
		1, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St",
		audit_loss_dollars_est=None,
	)
	assert "$" not in body


def test_touch1_uses_specific_dollar_hook_when_loss_estimate_present():
	_, body, _ = render_winback_touch(
		1, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St",
		audit_loss_dollars_est=4200,
	)
	assert "$4,200" in body


def test_touch3_includes_booking_url_when_provided():
	_, body, _ = render_winback_touch(
		3, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St",
		booking_url="https://calendar.example.com/book/acme",
	)
	assert "https://calendar.example.com/book/acme" in body


def test_touch3_falls_back_to_reply_when_no_booking_url():
	_, body, _ = render_winback_touch(
		3, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St",
		booking_url=None,
	)
	assert "http" not in body
	assert "reply" in body.lower()


def test_touch2_threads_as_reply_subject():
	subject1, _, _ = render_winback_touch(1, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St")
	subject2, _, _ = render_winback_touch(2, owner_name="Jane Doe", county_name="Hillsborough", property_address="1 Main St")
	assert subject2 == f"Re: {subject1}"


def test_greeting_falls_back_when_owner_name_missing():
	_, body, _ = render_winback_touch(1, owner_name="", county_name="Hillsborough", property_address="1 Main St")
	assert "Hi there," in body
