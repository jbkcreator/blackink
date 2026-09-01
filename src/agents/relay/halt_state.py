"""Relay halt data structures.

HaltScope identifies which level of the pipeline is frozen:
  GLOBAL   — all clients, all campaigns, all workers stop immediately.
  CLIENT   — one client_id stops; other clients continue.
  CAMPAIGN — one campaign_id stops; other campaigns for that client continue.

Scopes cascade when checking: a GLOBAL halt blocks everything; a CLIENT halt
blocks all campaigns for that client. is_halted() in halt_service.py applies
this cascade by checking every relevant scope for a given call context.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

SCOPE_GLOBAL = "GLOBAL"
SCOPE_CLIENT = "CLIENT"
SCOPE_CAMPAIGN = "CAMPAIGN"

VALID_SCOPES = (SCOPE_GLOBAL, SCOPE_CLIENT, SCOPE_CAMPAIGN)


@dataclass(frozen=True)
class HaltRecord:
    halt_id: int
    scope: str
    scope_id: Optional[str]
    reason: Optional[str]
    issued_by: str
    issued_at: datetime
