"""The single write path for provisioning a clients row (PR #37 review —
blocking finding: "Founding clients are not marked automatically").

No client-portal/self-serve onboarding flow exists anywhere in this repo yet
(same standing gap as the payment-auth onboarding token and the
calendar-connect link) — clients are provisioned by hand today (see the
various e2e/seed scripts under scripts/ and src/tasks/seed_demo_sandbox.py
that INSERT INTO clients directly). Until a real onboarding flow exists,
provision_client() is the intended write path for whoever/whatever creates a
client row — `founding` is a REQUIRED keyword argument with no default, so a
new call site cannot silently default a September founding client to
`founding=False` by omission. tests/test_billing_structural.py asserts this
signature has no default for `founding`.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


def provision_client(
	session: Session,
	*,
	client_id: str,
	display_name: str,
	founding: bool,
	plan_tier: str = "standard",
	is_active: bool = True,
) -> None:
	"""Inserts one clients row. ON CONFLICT DO NOTHING — re-running
	provisioning for an already-existing client_id is a no-op, never a
	silent overwrite of an operator-set founding flag."""
	session.execute(
		text(
			"INSERT INTO clients (client_id, display_name, is_active, plan_tier, founding) "
			"VALUES (:client_id, :display_name, :is_active, :plan_tier, :founding) "
			"ON CONFLICT (client_id) DO NOTHING"
		),
		{
			"client_id": client_id,
			"display_name": display_name,
			"is_active": is_active,
			"plan_tier": plan_tier,
			"founding": founding,
		},
	)
