"""Synthetic door_signed ingest (Subtask 1.2.2) — operator-only.

No nightly PMS sync exists in this repo (see migrations/apply_pms_agreements.py).
This route is the honest way to satisfy the DoD's "execute a test outcome
transaction" without pretending a sync exists: it lets an authenticated
operator record a SYNTHETIC door_signed event, which
src/services/settlement/charge.py's trigger-enforced guard (see
migrations/apply_settlement_ledger.py) refuses to let back a real charge
outside Stripe test mode.

Fail-closed like every other secret-gated route in this repo: an unset
SETTLEMENT_OPERATOR_API_KEY means every request is rejected, not
silently allowed through.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from config.settings import get_settings
from src.core.database import get_db_context
from src.services.settlement.ledger import open_settlement, record_door_signed

router = APIRouter(prefix="/api/v1/settlement", tags=["settlement"])


class DoorSignedRequest(BaseModel):
	client_id: str
	opportunity_id: str
	offer_code: str
	door_count: int = Field(ge=1, default=1)
	company_id: Optional[str] = None
	owner_contact_id: Optional[int] = None
	pms_property_ref: Optional[str] = None
	owner_domain: Optional[str] = None
	owner_email: Optional[str] = None


def _require_operator(x_settlement_operator_key: Optional[str] = Header(default=None)) -> None:
	settings = get_settings()
	expected = getattr(settings, "settlement_operator_api_key", None)
	if expected is None:
		raise HTTPException(status_code=503, detail="settlement operator route not configured")
	provided = x_settlement_operator_key or ""
	if not hmac.compare_digest(provided, expected.get_secret_value()):
		raise HTTPException(status_code=401, detail="invalid operator key")


@router.post("/door-signed")
def post_door_signed(body: DoorSignedRequest, x_settlement_operator_key: Optional[str] = Header(default=None)):
	_require_operator(x_settlement_operator_key)

	now = datetime.now(timezone.utc)
	with get_db_context(client_id=body.client_id) as session:
		pms_agreement_id = record_door_signed(
			session,
			client_id=body.client_id,
			opportunity_id=body.opportunity_id,
			door_signed_at=now,
			agreement_source="SYNTHETIC",
			company_id=body.company_id,
			owner_contact_id=body.owner_contact_id,
			pms_property_ref=body.pms_property_ref,
			door_count=body.door_count,
			owner_domain=body.owner_domain,
			owner_email=body.owner_email,
		)
		if pms_agreement_id is None:
			raise HTTPException(status_code=409, detail="duplicate door_signed agreement")

		transaction_id = open_settlement(
			session, client_id=body.client_id, pms_agreement_id=pms_agreement_id, offer_code=body.offer_code
		)
		if transaction_id is None:
			raise HTTPException(status_code=409, detail="settlement already exists for this opportunity/agreement")

	return {"pms_agreement_id": pms_agreement_id, "transaction_id": transaction_id}
