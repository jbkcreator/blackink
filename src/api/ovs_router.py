"""
Self-Serve OVS Audit Request endpoint.

Accepts form submissions from audit.getblackink.com, stores the inbound
lead in ovs_audit_requests, and returns an estimated turnaround time.

No auth guard — this is the public demand-gen front door (same posture as
/healthz). Rate-limiting and bot protection are handled at the CDN layer.
"""

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, field_validator

from src.core.database import get_db_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ovs", tags=["ovs"])


class AuditRequest(BaseModel):
    company_name: str
    domain: str
    contact_name: str | None = None
    email: EmailStr
    state: str | None = "FL"

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        # Strip protocol and trailing slashes so we store bare domain
        v = v.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if v.startswith(prefix):
                v = v[len(prefix):]
        return v.rstrip("/")

    @field_validator("company_name", "email")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field is required")
        return v.strip()


@router.post("/request", status_code=201)
def submit_audit_request(req: AuditRequest):
    """Store an inbound OVS audit request from the public landing page."""
    t0 = time.monotonic()
    try:
        with get_db_context() as db:
            t_db = time.monotonic()
            row = db.execute(
                __import__("sqlalchemy").text(
                    """
                    INSERT INTO ovs_audit_requests
                        (company_name, domain, contact_name, email, state, status)
                    VALUES
                        (:company_name, :domain, :contact_name, :email, :state, 'pending')
                    RETURNING id
                    """
                ),
                {
                    "company_name": req.company_name,
                    "domain": req.domain,
                    "contact_name": req.contact_name,
                    "email": req.email,
                    "state": req.state,
                },
            ).fetchone()
            db.commit()
            request_id = row[0]
    except Exception:
        logger.exception("[ovs] failed to store audit request for %s", req.domain)
        raise HTTPException(status_code=500, detail="Could not submit request. Please try again.")

    db_ms = (time.monotonic() - t_db) * 1000
    total_ms = (time.monotonic() - t0) * 1000
    logger.info("[ovs] new audit request #%d — %s (%s) db=%.1f ms total=%.1f ms",
                request_id, req.company_name, req.domain, db_ms, total_ms)

    return {
        "request_id": request_id,
        "status": "pending",
        "estimated_completion": "24h",
        "message": f"Your OVS report for {req.domain} is queued. We'll email {req.email} within 24 hours.",
    }
