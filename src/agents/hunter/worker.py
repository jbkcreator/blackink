"""Hunter entity-resolution worker — nightly sweep.

Reads all companies with entity_type='llc_portfolio_owner' and
owner_entity_id IS NULL, clusters them into canonical owner entities using
rapidfuzz name similarity (entity_resolution.resolve_batch), then writes
owner_entities + owner_entity_links rows and back-fills
companies.owner_entity_id.

Role: blackink_system (BYPASSRLS) — sweeps all tenants' companies without
a client_id filter. NEVER import this from src/api/ — use the nightly cron
entry point (src/tasks/hunter_nightly_sweep.py) instead.

Read / write pattern (no cross-table joins during resolution):
  1. Read a batch of company rows (company_id, company_name, domain,
     door_count_est) from the DB — no joins.
  2. Call entity_resolution.resolve_batch() — pure in-memory.
  3. Write the resulting clusters back in a separate transaction:
     INSERT owner_entity → INSERT owner_entity_links → UPDATE companies.

The query always starts from offset 0 because resolved companies are
filtered out by WHERE owner_entity_id IS NULL. After the write the pool
shrinks, so the loop naturally terminates.
"""
from __future__ import annotations

import logging
from typing import List

from sqlalchemy import text

from src.agents.hunter.entity_resolution import CompanyRecord, ResolvedCluster, resolve_batch
from src.agents.hunter.kill_switch import hunter_should_stop
from src.core.database import Database

logger = logging.getLogger(__name__)

BATCH_SIZE: int = 500


def _fetch_batch(sess, limit: int) -> List[CompanyRecord]:
    rows = sess.execute(
        text("""
            SELECT company_id, company_name, domain, door_count_est
            FROM companies
            WHERE entity_type = 'llc_portfolio_owner'
              AND owner_entity_id IS NULL
            ORDER BY company_id
            LIMIT :limit
        """),
        {"limit": limit},
    ).fetchall()
    return [
        CompanyRecord(
            company_id=r[0],
            company_name=r[1],
            domain=r[2],
            door_count_est=r[3],
        )
        for r in rows
    ]


def _write_cluster(sess, cluster: ResolvedCluster) -> None:
    door_count = sum(c.door_count_est or 0 for c in cluster.companies) or None

    result = sess.execute(
        text("""
            INSERT INTO owner_entities
                (canonical_name, entity_type, portfolio_door_count_est,
                 confidence_score, verification_status)
            VALUES
                (:name, 'LLC', :doors, :conf, 'unverified')
            RETURNING id
        """),
        {
            "name": cluster.canonical_name,
            "doors": door_count,
            "conf": cluster.cluster_confidence,
        },
    )
    entity_id = result.scalar()

    for company in cluster.companies:
        sess.execute(
            text("""
                INSERT INTO owner_entity_links
                    (owner_entity_id, source_table, source_id,
                     match_confidence, match_method)
                VALUES
                    (:eid, 'companies', :cid, :conf, :method)
                ON CONFLICT (source_table, source_id) DO NOTHING
            """),
            {
                "eid": entity_id,
                "cid": company.company_id,
                "conf": cluster.cluster_confidence,
                "method": cluster.match_method,
            },
        )
        sess.execute(
            text("""
                UPDATE companies
                SET owner_entity_id = :eid
                WHERE company_id = :cid
                  AND owner_entity_id IS NULL
            """),
            {"eid": entity_id, "cid": company.company_id},
        )


def run_sweep() -> int:
    """Execute one full resolution sweep. Returns total companies resolved.

    Safe to call repeatedly — already-resolved companies are skipped by the
    WHERE owner_entity_id IS NULL filter. If a Relay GLOBAL halt is active,
    returns 0 immediately without touching the DB.
    """
    if hunter_should_stop():
        return 0

    db = Database()
    total_companies = 0
    total_clusters = 0

    while True:
        with db.system_session_scope() as read_sess:
            batch = _fetch_batch(read_sess, BATCH_SIZE)

        if not batch:
            break

        clusters = resolve_batch(batch)

        with db.system_session_scope() as write_sess:
            for cluster in clusters:
                _write_cluster(write_sess, cluster)

        total_companies += len(batch)
        total_clusters += len(clusters)
        logger.info(
            "hunter: wrote %d clusters from %d companies (running total: %d)",
            len(clusters), len(batch), total_companies,
        )

        if len(batch) < BATCH_SIZE:
            break

    logger.info(
        "hunter: sweep complete — %d companies → %d entities",
        total_companies, total_clusters,
    )
    return total_companies
