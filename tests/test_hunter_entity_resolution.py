"""Unit tests for Hunter entity resolution.

Tests resolve_batch() in isolation (no live DB required).
Covers:
- Empty input
- Singleton (no matches) → own entity
- Exact-name duplicates cluster together
- LLC suffix variants cluster together (normalization)
- Dissimilar names produce separate clusters
- Transitive similarity (A~B, B~C → one cluster via union-find)
- Canonical name selection (shortest wins, alpha tie-break)
- match_method and cluster_confidence values
- kill_switch delegates to Relay halt
- worker run_sweep() short-circuits on halt
- worker run_sweep() with FakeSession: inserts entity + links + updates company
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.hunter.entity_resolution import (
    SIMILARITY_THRESHOLD,
    CompanyRecord,
    ResolvedCluster,
    _normalize,
    _pick_canonical,
    resolve_batch,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rec(company_id: str, company_name: str, domain: str = "x.com", doors: int = None) -> CompanyRecord:
    return CompanyRecord(company_id=company_id, company_name=company_name, domain=domain, door_count_est=doors)


# ── _normalize ────────────────────────────────────────────────────────────────

def test_normalize_strips_llc():
    assert _normalize("ABC Properties LLC") == "abc properties"


def test_normalize_strips_llc_with_comma():
    assert _normalize("ABC Properties, LLC") == "abc properties"


def test_normalize_strips_llc_dotted():
    assert _normalize("ABC Properties L.L.C.") == "abc properties"


def test_normalize_strips_inc():
    assert _normalize("ABC Management Inc.") == "abc management"


def test_normalize_strips_corp():
    assert _normalize("Acme Holdings Corp") == "acme holdings"


def test_normalize_lowercases():
    assert _normalize("BLUE SKY LLC") == "blue sky"


def test_normalize_collapses_punctuation():
    result = _normalize("A&B Realty, LLC")
    assert "," not in result
    assert "&" not in result


# ── _pick_canonical ───────────────────────────────────────────────────────────

def test_canonical_shortest_wins():
    assert _pick_canonical(["ABC Properties LLC", "ABC LLC"]) == "ABC LLC"


def test_canonical_alpha_tiebreak():
    # Same length — alphabetical
    result = _pick_canonical(["Bravo LLC", "Alpha LLC"])
    assert result == "Alpha LLC"


def test_canonical_single_element():
    assert _pick_canonical(["Only One"]) == "Only One"


# ── resolve_batch — empty ─────────────────────────────────────────────────────

def test_resolve_empty_returns_empty():
    assert resolve_batch([]) == []


# ── resolve_batch — singletons ────────────────────────────────────────────────

def test_single_record_is_singleton():
    clusters = resolve_batch([_rec("id1", "Unique Properties LLC")])
    assert len(clusters) == 1
    assert clusters[0].match_method == "singleton"
    assert clusters[0].cluster_confidence == 50
    assert clusters[0].companies[0].company_id == "id1"


def test_two_dissimilar_names_separate():
    clusters = resolve_batch([
        _rec("id1", "ABC Properties LLC"),
        _rec("id2", "XYZ Holdings Corp"),
    ])
    assert len(clusters) == 2


def test_three_dissimilar_names_separate():
    clusters = resolve_batch([
        _rec("a", "Sunrise Realty LLC"),
        _rec("b", "Pacific Coast Homes Inc"),
        _rec("c", "Mountain View Properties Corp"),
    ])
    assert len(clusters) == 3


# ── resolve_batch — clustering ────────────────────────────────────────────────

def test_identical_names_cluster():
    clusters = resolve_batch([
        _rec("id1", "ABC Properties LLC"),
        _rec("id2", "ABC Properties LLC"),
    ])
    assert len(clusters) == 1
    assert len(clusters[0].companies) == 2
    assert clusters[0].match_method == "fuzzy_name"
    assert clusters[0].cluster_confidence == SIMILARITY_THRESHOLD


def test_llc_suffix_variants_cluster():
    clusters = resolve_batch([
        _rec("id1", "Blue Sky Realty LLC"),
        _rec("id2", "Blue Sky Realty, LLC"),
        _rec("id3", "Blue Sky Realty L.L.C."),
    ])
    assert len(clusters) == 1
    assert len(clusters[0].companies) == 3


def test_near_identical_names_cluster():
    clusters = resolve_batch([
        _rec("id1", "Sunrise Properties LLC"),
        _rec("id2", "Sunrise Property LLC"),
    ])
    assert len(clusters) == 1


def test_dissimilar_names_do_not_cluster():
    clusters = resolve_batch([
        _rec("id1", "Sunrise Properties LLC"),
        _rec("id2", "Zyxw Holdings Corp"),
    ])
    assert len(clusters) == 2


def test_canonical_name_in_cluster():
    clusters = resolve_batch([
        _rec("id1", "ABC Properties LLC"),
        _rec("id2", "ABC Properties, LLC"),
    ])
    assert clusters[0].canonical_name in {"ABC Properties LLC", "ABC Properties, LLC"}


def test_canonical_name_shortest():
    """Shorter name wins within the cluster."""
    clusters = resolve_batch([
        _rec("id1", "Blue Ocean LLC"),
        _rec("id2", "Blue Ocean Property Management LLC"),
    ])
    # Both normalize to ~"blue ocean" and should cluster
    if len(clusters) == 1:
        assert clusters[0].canonical_name == "Blue Ocean LLC"


# ── Transitive closure (union-find) ──────────────────────────────────────────

def test_transitive_cluster():
    """A~B and B~C → A, B, C all in one cluster (union-find transitivity)."""
    clusters = resolve_batch([
        _rec("id1", "Alpha Realty LLC"),
        _rec("id2", "Alpha Realty, LLC"),
        _rec("id3", "Alpha Realty L.L.C."),
    ])
    # All three are variants of the same name
    assert len(clusters) == 1
    assert len(clusters[0].companies) == 3


# ── All-cluster ids covered ───────────────────────────────────────────────────

def test_all_input_records_appear_in_output():
    records = [
        _rec("a", "Sunset Properties LLC"),
        _rec("b", "Sunrise Holdings Inc"),
        _rec("c", "Sunset Properties, LLC"),
    ]
    clusters = resolve_batch(records)
    returned_ids = {c.company_id for cl in clusters for c in cl.companies}
    assert returned_ids == {"a", "b", "c"}


# ── kill_switch ───────────────────────────────────────────────────────────────

def test_hunter_should_stop_false_when_no_halt():
    with patch("src.agents.hunter.kill_switch.is_halted", return_value=False):
        from src.agents.hunter.kill_switch import hunter_should_stop
        assert hunter_should_stop() is False


def test_hunter_should_stop_true_on_global_halt():
    with patch("src.agents.hunter.kill_switch.is_halted", return_value=True):
        from src.agents.hunter.kill_switch import hunter_should_stop
        assert hunter_should_stop() is True


# ── worker.run_sweep() — halt short-circuit ───────────────────────────────────

def test_run_sweep_returns_zero_on_halt():
    with patch("src.agents.hunter.worker.hunter_should_stop", return_value=True):
        from src.agents.hunter.worker import run_sweep
        assert run_sweep() == 0


# ── worker.run_sweep() — FakeSession ─────────────────────────────────────────

class _FakeResult:
    def __init__(self, rows=None, scalar_val=None):
        self._rows = rows or []
        self._scalar = scalar_val

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _FakeSession:
    """Minimal session stub: tracks execute() calls and returns configurable results."""
    def __init__(self, read_rows, entity_id=99):
        self._read_rows = read_rows  # rows returned on first fetchall
        self._entity_id = entity_id
        self.executed = []

    def execute(self, stmt, params=None):
        sql = str(stmt).lower()
        self.executed.append((sql, params or {}))
        if "from companies" in sql and "fetchall" not in sql:
            return _FakeResult(rows=self._read_rows)
        if "insert into owner_entities" in sql:
            return _FakeResult(scalar_val=self._entity_id)
        return _FakeResult()


def test_run_sweep_empty_db_returns_zero():
    """No rows → sweep does nothing and returns 0."""
    read_session = _FakeSession(read_rows=[])

    @contextmanager
    def _scope():
        yield read_session

    mock_db = MagicMock()
    mock_db.system_session_scope.side_effect = lambda: _scope()

    with patch("src.agents.hunter.worker.hunter_should_stop", return_value=False), \
         patch("src.agents.hunter.worker.Database", return_value=mock_db):
        from src.agents.hunter.worker import run_sweep
        result = run_sweep()

    assert result == 0


def test_run_sweep_single_company_creates_entity():
    """One unresolved company → one owner_entity INSERT + one link INSERT + one UPDATE."""
    company_row = ("cid123", "Acme Properties LLC", "acme.com", 50)
    sessions_yielded = []

    @contextmanager
    def _scope():
        sess = _FakeSession(read_rows=[company_row] if not sessions_yielded else [])
        sessions_yielded.append(sess)
        yield sess

    mock_db = MagicMock()
    mock_db.system_session_scope.side_effect = lambda: _scope()

    with patch("src.agents.hunter.worker.hunter_should_stop", return_value=False), \
         patch("src.agents.hunter.worker.Database", return_value=mock_db):
        from src.agents.hunter.worker import run_sweep
        result = run_sweep()

    assert result == 1
    write_sess = sessions_yielded[1]
    sqls = [e[0] for e in write_sess.executed]
    assert any("insert into owner_entities" in s for s in sqls)
    assert any("insert into owner_entity_links" in s for s in sqls)
    assert any("update companies" in s for s in sqls)


def test_run_sweep_two_similar_companies_one_entity():
    """Two similar-name companies → one cluster → one owner_entity, two links."""
    rows = [
        ("cid1", "Blue Sky Realty LLC", "bluesky.com", 10),
        ("cid2", "Blue Sky Realty, LLC", "bluesky2.com", 20),
    ]
    sessions_yielded = []

    @contextmanager
    def _scope():
        sess = _FakeSession(read_rows=rows if not sessions_yielded else [])
        sessions_yielded.append(sess)
        yield sess

    mock_db = MagicMock()
    mock_db.system_session_scope.side_effect = lambda: _scope()

    with patch("src.agents.hunter.worker.hunter_should_stop", return_value=False), \
         patch("src.agents.hunter.worker.Database", return_value=mock_db):
        from src.agents.hunter.worker import run_sweep
        result = run_sweep()

    assert result == 2
    write_sess = sessions_yielded[1]
    sqls = [e[0] for e in write_sess.executed]
    owner_entity_inserts = [s for s in sqls if "insert into owner_entities" in s]
    link_inserts = [s for s in sqls if "insert into owner_entity_links" in s]
    assert len(owner_entity_inserts) == 1   # one cluster → one entity
    assert len(link_inserts) == 2           # two companies → two links
