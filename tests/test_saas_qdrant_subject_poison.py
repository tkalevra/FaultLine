"""Qdrant POISON TEST — the VECTOR-side acceptance gate for the per-account isolation grain.

Maps to ``DEV/DESIGN-per-account-grain-and-api-subject.md`` §3.2 (the Qdrant twin of the §2
connection poison test). The guard under test is ``saas/qdrant_subject_query.py``.

The gates:
  #1 Unbound search/scroll/delete/upsert THROWS — GREEN now (the throw precedes any HTTP call).
  #2 Filter-injection proven                     — GREEN now (fake client records the emitted body).
  #3 Grep-audit lint                             — GREEN now (scoped to the builder module in Phase 1a).
  #4 Cortex firewall preserved (§3.4)            — GREEN now (builder refuses flagent collections).
  #5 Cross-subject empty (live Qdrant + wiring)  — SKIP (gated on the wiring phase).

Run: python3 -m pytest tests/test_saas_qdrant_subject_poison.py -q   (tests/ is gitignored → git add -f)
"""

import os
import re
import uuid

import pytest

from saas import subject_cursor as sc
from saas.subject_cursor import (
    UnboundSubjectError,
    reset_bound_subject,
    set_bound_subject,
)
from saas import qdrant_subject_query as qs
from saas.qdrant_subject_query import (
    PAYLOAD_ACCOUNT_KEY,
    PAYLOAD_SUBJECT_KEY,
    QdrantSubjectQuery,
    build_partition_filter,
    derive_model_collection,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─────────────────────────────────────────────────────────────────────────────────────
# Fake Qdrant HTTP client — records the exact (url, body) of every request so we can prove
# the partition filter is injected WITHOUT a live Qdrant.
# ─────────────────────────────────────────────────────────────────────────────────────
class _FakeResp:
    status_code = 200

    def json(self):
        return {"result": {"points": []}}


class _FakeQdrantClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []  # (method, url, json_body)

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return _FakeResp()

    def put(self, url, json=None, timeout=None):
        self.calls.append(("PUT", url, json))
        return _FakeResp()


class _ExplodingClient:
    """Any use is a test failure — proves the unbound guard trips BEFORE any HTTP call."""

    def post(self, *a, **k):
        raise AssertionError("Qdrant client must NOT be reached when unbound")

    def put(self, *a, **k):
        raise AssertionError("Qdrant client must NOT be reached when unbound")


@pytest.fixture(autouse=True)
def _clean_binding():
    """Every test starts + ends with NO bound subject (shared ContextVar with the PG side)."""
    try:
        sc._bound_subject.set(None)
    except Exception:
        pass
    yield
    try:
        sc._bound_subject.set(None)
    except Exception:
        pass


def _bind():
    acct = str(uuid.uuid4())
    subj = str(uuid.uuid4())
    tok = set_bound_subject(acct, "faultline_acct_test", subj)
    return acct, subj, tok


def _q(client):
    return QdrantSubjectQuery(collection="faultline_m_nomic768", qdrant_url="http://q:6333", client=client)


# ─────────────────────────────────────────────────────────────────────────────────────
# Collection naming + cortex firewall (§3.1 / §3.4) — GREEN now, no DB
# ─────────────────────────────────────────────────────────────────────────────────────
def test_derive_model_collection_shape():
    assert derive_model_collection("nomic768") == "faultline_m_nomic768"


def test_derive_model_collection_rejects_unsafe_key():
    # Genuinely-illegal chars (dash/space/punct) are refused even after normalization.
    for bad in ("nomic-768", "a b", "$$", "faultline;drop", "m/../x"):
        with pytest.raises(ValueError):
            derive_model_collection(bad)


def test_derive_model_collection_normalizes_case_and_defaults_empty():
    # Case is normalized (convenience), empty falls back to the env/default key.
    assert derive_model_collection("NOMIC768") == "faultline_m_nomic768"
    assert derive_model_collection("").startswith("faultline_m_")


def test_builder_refuses_cortex_collection():
    """§3.4 firewall: the per-account builder can NEVER be pointed at a flagent_ (cortex)
    collection — the disjoint-prefix invariant is enforced at construction."""
    for bad in ("flagent-abc", "flagent_m_nomic768", "flagent_deadbeef"):
        with pytest.raises(ValueError):
            QdrantSubjectQuery(collection=bad, client=_FakeQdrantClient())


def test_builder_rejects_non_model_collection():
    for bad in ("faultline-1234", "random", "faultline_acct_x"):
        with pytest.raises(ValueError):
            QdrantSubjectQuery(collection=bad, client=_FakeQdrantClient())


# ─────────────────────────────────────────────────────────────────────────────────────
# POISON #1 — UNBOUND THROWS (§3.2). MUST pass now (throw precedes any HTTP call).
# ─────────────────────────────────────────────────────────────────────────────────────
def test_poison_1_unbound_build_filter_throws():
    with pytest.raises(UnboundSubjectError):
        build_partition_filter()


def test_poison_1_unbound_search_throws_before_http():
    q = _q(_ExplodingClient())
    with pytest.raises(UnboundSubjectError):
        q.search([0.1] * 8)


def test_poison_1_unbound_scroll_delete_upsert_throw_before_http():
    q = _q(_ExplodingClient())
    with pytest.raises(UnboundSubjectError):
        q.scroll()
    with pytest.raises(UnboundSubjectError):
        q.delete("staged_facts", 1)
    with pytest.raises(UnboundSubjectError):
        q.upsert(1, [0.1] * 8, {"rel_type": "owns"})
    with pytest.raises(UnboundSubjectError):
        q.scroll_account_scoped()


# ─────────────────────────────────────────────────────────────────────────────────────
# POISON #2 — FILTER-INJECTION PROVEN (§3.2). MUST pass now (fake client).
# ─────────────────────────────────────────────────────────────────────────────────────
def _must_of(body: dict) -> list:
    return body["filter"]["must"]


def _has_cond(must: list, key: str, value: str) -> bool:
    return any(c.get("key") == key and c.get("match", {}).get("value") == value for c in must)


def test_poison_2_search_injects_both_partition_keys():
    acct, subj, tok = _bind()
    try:
        client = _FakeQdrantClient()
        q = _q(client)
        q.search([0.1] * 8, limit=5)
        _, url, body = client.calls[-1]
        must = _must_of(body)
        assert _has_cond(must, PAYLOAD_ACCOUNT_KEY, acct)
        assert _has_cond(must, PAYLOAD_SUBJECT_KEY, subj)
        assert "faultline_m_nomic768" in url
    finally:
        reset_bound_subject(tok)


def test_poison_2_scroll_and_delete_inject_partition_keys():
    acct, subj, tok = _bind()
    try:
        client = _FakeQdrantClient()
        q = _q(client)
        q.scroll(limit=10)
        must = _must_of(client.calls[-1][2])
        assert _has_cond(must, PAYLOAD_ACCOUNT_KEY, acct)
        assert _has_cond(must, PAYLOAD_SUBJECT_KEY, subj)

        q.delete("staged_facts", 42)
        must = _must_of(client.calls[-1][2])
        assert _has_cond(must, PAYLOAD_ACCOUNT_KEY, acct)
        assert _has_cond(must, PAYLOAD_SUBJECT_KEY, subj)
        # collision-safe predicates ANDed on top, never replacing the partition
        assert _has_cond(must, "source_table", "staged_facts")
        assert any(c.get("key") == "fact_id" for c in must)
    finally:
        reset_bound_subject(tok)


def test_poison_2_caller_cannot_drop_or_override_partition():
    """A caller passing their OWN account_id/subject_id in extra_must cannot widen scope: the
    bound partition conditions are still present (Qdrant ANDs the whole must[]), so the query
    can only ever NARROW, never escape the bound (account, subject)."""
    acct, subj, tok = _bind()
    try:
        client = _FakeQdrantClient()
        q = _q(client)
        q.search(
            [0.1] * 8,
            extra_must=[{"key": PAYLOAD_ACCOUNT_KEY, "match": {"value": "ATTACKER_ACCT"}},
                        {"key": PAYLOAD_SUBJECT_KEY, "match": {"value": "ATTACKER_SUBJ"}}],
        )
        must = _must_of(client.calls[-1][2])
        # the BOUND values are still present (leading conditions) — the attacker's extra
        # conditions are additional ANDs that can only further restrict, never replace.
        assert must[0] == {"key": PAYLOAD_ACCOUNT_KEY, "match": {"value": acct}}
        assert must[1] == {"key": PAYLOAD_SUBJECT_KEY, "match": {"value": subj}}
    finally:
        reset_bound_subject(tok)


def test_poison_2_upsert_stamps_partition_uneditable():
    """Upsert stamps the BOUND (account_id, subject_id) — a caller-supplied value in the payload
    cannot override them, so a point can never be written into the wrong partition."""
    acct, subj, tok = _bind()
    try:
        client = _FakeQdrantClient()
        q = _q(client)
        q.upsert(1, [0.1] * 8, {"rel_type": "owns",
                                PAYLOAD_ACCOUNT_KEY: "SPOOF_ACCT",
                                PAYLOAD_SUBJECT_KEY: "SPOOF_SUBJ"})
        method, url, body = client.calls[-1]
        assert method == "PUT"
        payload = body["points"][0]["payload"]
        assert payload[PAYLOAD_ACCOUNT_KEY] == acct  # bound wins
        assert payload[PAYLOAD_SUBJECT_KEY] == subj
    finally:
        reset_bound_subject(tok)


def test_poison_2_account_scope_hatch_keeps_account_filter():
    """The single named escape hatch drops ONLY the subject predicate and STILL injects the
    account predicate — never an unfiltered 'scroll all'."""
    acct, subj, tok = _bind()
    try:
        client = _FakeQdrantClient()
        q = _q(client)
        q.scroll_account_scoped(limit=50)
        must = _must_of(client.calls[-1][2])
        assert _has_cond(must, PAYLOAD_ACCOUNT_KEY, acct)
        assert not _has_cond(must, PAYLOAD_SUBJECT_KEY, subj)  # subject intentionally dropped
        assert len(must) >= 1  # account condition always present — never empty
    finally:
        reset_bound_subject(tok)


# ─────────────────────────────────────────────────────────────────────────────────────
# POISON #3 — GREP-AUDIT LINT (§3.2). GREEN now.
#
# No raw Qdrant request endpoint (points/query|scroll|search|delete or points PUT) may exist on
# a data-plane module outside the builder. The sweep (wiring phase) has NOT happened, so the
# data-plane module set is intentionally EMPTY here — the machinery runs, asserts clean, and is
# ready to expand to embedder.py / main.py once those route through QdrantSubjectQuery.
# ─────────────────────────────────────────────────────────────────────────────────────
_QDRANT_ENDPOINT_RE = re.compile(r"points/(query|scroll|search|delete)|/points\b")

BUILDER_MODULE = os.path.join(_REPO_ROOT, "saas", "qdrant_subject_query.py")

# Modules whose per-account Qdrant access MUST route through the builder. EMPTY in this phase
# (nothing swept yet). The wiring phase adds src/re_embedder/embedder.py + src/api/main.py's
# per-account vector lane as each is migrated; the lint then guards them from regressing.
DATA_PLANE_MODULES: list[str] = []


def find_raw_qdrant_calls(files: list[str]) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in files:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                if _QDRANT_ENDPOINT_RE.search(line):
                    hits.append((path, i, line.strip()))
    return hits


def test_poison_3_grep_audit_data_plane_clean():
    violations = find_raw_qdrant_calls(DATA_PLANE_MODULES)
    assert violations == [], (
        "Raw Qdrant request found on data-plane modules outside the builder — route through "
        f"QdrantSubjectQuery: {violations}"
    )


def test_poison_3_builder_is_the_sanctioned_transport_home():
    """The builder module is the sanctioned home for the raw Qdrant HTTP calls, and they live
    ONLY behind the filtered ``_post``/``_put`` transport (which every public method feeds
    through ``build_partition_filter``). Assert there is no public method that emits an
    unfiltered request body."""
    with open(BUILDER_MODULE, "r", encoding="utf-8") as fh:
        src = fh.read()
    # The named account-scope hatch exists exactly once and is the only subject-less path.
    assert src.count(f"def {qs.ACCOUNT_SCOPE_METHOD}(") == 1
    # Transport is private (underscore-prefixed) — no public bare def post/put on the class.
    assert re.search(r"\n    def (post|put|search_all|scroll_all)\s*\(", src) is None


# ─────────────────────────────────────────────────────────────────────────────────────
# POISON #5 — CROSS-SUBJECT EMPTY (§3.2). SKELETON, gated on the wiring phase. Do NOT fake green.
# ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.skip(reason="gated on wiring phase — needs a live Qdrant + upsert/query wired onto the builder")
def test_poison_5_cross_subject_returns_zero_a_points():
    """§3.2. Upsert subject A's vector into the shared model collection via the builder; bind
    subject B (same account) and search → assert ZERO of A's points come back. Then bind a
    DIFFERENT account and search → still zero. Proves the payload partition filter is a real
    boundary against a live Qdrant.

    Turns GREEN when the wiring phase: (a) upserts through QdrantSubjectQuery.upsert (stamping
    the partition), (b) queries through QdrantSubjectQuery.search, (c) runs against a live
    Qdrant with payload indexes on account_id + subject_id.
    """
    coll = derive_model_collection("nomic768")
    acct = str(uuid.uuid4())
    subj_a, subj_b = str(uuid.uuid4()), str(uuid.uuid4())

    # bind A → upsert a point
    tok = set_bound_subject(acct, "faultline_acct_poisontest", subj_a)
    try:
        q = QdrantSubjectQuery(collection=coll)  # real httpx
        # q.upsert(<point_id>, <vector>, {"rel_type": "owns", "fact_id": 1, "source_table": "staged_facts"})
        ...
    finally:
        reset_bound_subject(tok)

    # bind B (same account) → search must not see A's point
    tok = set_bound_subject(acct, "faultline_acct_poisontest", subj_b)
    try:
        q = QdrantSubjectQuery(collection=coll)
        resp = q.search([0.0] * 768, limit=50)  # noqa: F841
        # points = resp.json()["result"]["points"]
        # assert all(p["payload"]["subject_id"] == subj_b for p in points)
        ...
    finally:
        reset_bound_subject(tok)
