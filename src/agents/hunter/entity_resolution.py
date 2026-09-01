"""Hunter — LLC entity resolution via fuzzy name clustering.

Takes a batch of companies with entity_type='llc_portfolio_owner' and
clusters them into canonical owner entities using rapidfuzz name similarity.

Algorithm:
  1. Normalize each company_name (strip legal suffixes, lowercase).
  2. Union-Find over all pairwise WRatio scores >= SIMILARITY_THRESHOLD
     (transitive closure: A~B, B~C → A,B,C in one cluster).
  3. Each connected component → one ResolvedCluster with a canonical name
     (shortest string in the component, which tends to be the root brand).
  4. Singletons (no match found) become their own single-member cluster,
     marked match_method='singleton'.

No DB access — pure in-memory; the worker handles reads and writes separately
so this module is testable without a live Postgres connection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from rapidfuzz import fuzz

SIMILARITY_THRESHOLD: int = 85  # WRatio score (0–100) for same-entity match

_LEGAL_SUFFIXES = (
    " llc", ", llc", " l.l.c.", " l.l.c", " inc", ", inc", " inc.",
    " corp", ", corp", " corp.", " co.", " co", " ltd", ", ltd", " ltd.",
    " lp", " lp.", " llp", " llp.", " limited", " properties",
)


def _normalize(name: str) -> str:
    """Lowercase, strip common legal suffixes, collapse punctuation.

    Applied only for similarity comparison — the original name is kept for
    display and for picking the canonical cluster name.
    """
    n = name.lower().strip()
    for suffix in sorted(_LEGAL_SUFFIXES, key=len, reverse=True):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
            break
    n = re.sub(r"[.,;:!?()\[\]&']", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _pick_canonical(names: List[str]) -> str:
    """Canonical name = shortest string (most likely the root brand name).

    Tie-break: alphabetical so the result is deterministic across runs.
    """
    return min(names, key=lambda n: (len(n), n.lower()))


@dataclass
class CompanyRecord:
    company_id: str
    company_name: str
    domain: str
    door_count_est: Optional[int]


@dataclass
class ResolvedCluster:
    canonical_name: str
    companies: List[CompanyRecord] = field(default_factory=list)
    match_method: str = "singleton"      # 'singleton' | 'fuzzy_name'
    cluster_confidence: int = 50         # 0–100; 50 = no evidence, 85 = threshold score


def resolve_batch(records: List[CompanyRecord]) -> List[ResolvedCluster]:
    """Cluster records into owner-entity groups.

    Pure in-memory — does not query the database. Callers must not pass
    records that already have an owner_entity_id (the worker filters them
    out in the SQL read).

    Returns one ResolvedCluster per entity group, including singletons.
    Order within the return list is arbitrary.
    """
    n = len(records)
    if n == 0:
        return []

    normalized = [_normalize(r.company_name) for r in records]

    # Union-Find with path compression
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            score = fuzz.WRatio(normalized[i], normalized[j])
            if score >= SIMILARITY_THRESHOLD:
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    clusters: List[ResolvedCluster] = []
    for indices in groups.values():
        cluster_records = [records[i] for i in indices]
        canonical = _pick_canonical([r.company_name for r in cluster_records])
        is_singleton = len(cluster_records) == 1
        clusters.append(
            ResolvedCluster(
                canonical_name=canonical,
                companies=cluster_records,
                match_method="singleton" if is_singleton else "fuzzy_name",
                cluster_confidence=50 if is_singleton else SIMILARITY_THRESHOLD,
            )
        )

    return clusters
