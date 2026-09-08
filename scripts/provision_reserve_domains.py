"""Provision reserve sending-domains with their owning client_id + cluster_label
(PR #35 review #2 — the deliverability sentinel only promotes a reserve that
matches the degraded domain's cluster AND tenant, so global/NULL-scoped reserves
never fire a swap).

This is the operator runbook step run at pilot setup, once the client's domain
inventory and its cluster assignments are known. It:
  1. Assigns each named reserve domain a client_id + cluster_label.
  2. Marks it is_reserve=TRUE, quarantine_state='reserve'.
  3. VERIFIES the target cluster has at least one warmed mailbox (a reserve with
     no warmed capacity can't actually rescue a swap) and warns if not.

Edit ASSIGNMENTS below with the real inventory, then:

    PYTHONPATH=. python scripts/provision_reserve_domains.py          # dry-run
    PYTHONPATH=. python scripts/provision_reserve_domains.py --apply  # write

Nothing is written without --apply.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

# domain → (client_id, cluster_label). Fill in from the client's real inventory.
# client_id=None + cluster_label=None is the internal self-marketing pool.
ASSIGNMENTS: dict[str, tuple[str | None, str | None]] = {
    # "acme-reserve-1.com": ("client_acme", "acme-cluster-1"),
}


def _warmed_capacity(session, client_id, cluster_label) -> int:
    return session.execute(
        text(
            "SELECT COUNT(*) FROM mailboxes m "
            "JOIN sending_domains sd ON sd.id = m.domain_id "
            "WHERE m.warmup_status = 'warmed' "
            "  AND sd.cluster_label IS NOT DISTINCT FROM :cl "
            "  AND sd.client_id IS NOT DISTINCT FROM :c"
        ),
        {"cl": cluster_label, "c": client_id},
    ).scalar() or 0


def main(apply: bool) -> int:
    if not ASSIGNMENTS:
        print("No ASSIGNMENTS configured — edit the ASSIGNMENTS dict with the real inventory.")
        return 1

    with get_owner_db_context() as session:
        for domain, (client_id, cluster_label) in ASSIGNMENTS.items():
            exists = session.execute(
                text("SELECT id FROM sending_domains WHERE domain = :d"), {"d": domain}
            ).scalar()
            cap = _warmed_capacity(session, client_id, cluster_label)
            cap_note = "" if cap > 0 else "  ⚠ NO warmed mailbox capacity in this cluster/tenant"
            print(f"{'APPLY' if apply else 'DRY '} {domain} → client_id={client_id} cluster={cluster_label} "
                  f"(exists={bool(exists)} warmed_capacity={cap}){cap_note}")
            if not apply:
                continue
            if exists:
                session.execute(
                    text(
                        "UPDATE sending_domains SET client_id = :c, cluster_label = :cl, "
                        "is_reserve = TRUE, quarantine_state = 'reserve' WHERE domain = :d"
                    ),
                    {"c": client_id, "cl": cluster_label, "d": domain},
                )
            else:
                session.execute(
                    text(
                        "INSERT INTO sending_domains (domain, client_id, cluster_label, is_reserve, quarantine_state) "
                        "VALUES (:d, :c, :cl, TRUE, 'reserve')"
                    ),
                    {"d": domain, "c": client_id, "cl": cluster_label},
                )
        if apply:
            session.commit()
            print("provision_reserve_domains: applied")
        else:
            print("provision_reserve_domains: dry-run only (pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
