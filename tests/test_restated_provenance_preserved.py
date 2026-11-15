"""RESTATED — a re-mined USER-STATED turn must come back Class A, not demoted.

THE DEFECT THIS PINS
`reextract_episodic` re-mines a retained turn from `episodic_log` and used to re-ingest it
with `source="reextract"`, which falls through the /ingest provenance router's else-branch
to `fact_provenance="llm_inferred"`. So a fact the USER STATED came back DEMOTED to Class
B/C instead of the Class A it earned the first time. That was a footnote while re-mining was
rare; the no-brain skip lane makes it a NORMAL recovery path (every turn captured while a
tenant had no brain bound is re-read once an endpoint is bound), so under the old router ALL
of it returned demoted.

OWNER RULING (binding): "Re-stated should be able to go from B→A. User is truth should be
respected." A fact does not become less true because OUR pipeline had to read it twice.

THE SAFETY PROPERTY — PRESERVING, NEVER ELEVATING
The fix re-ingests under the turn's ORIGINAL ingest source, resolved from
`episodic_log.source`. Only origin lanes whose LIVE ingest provably routed to user_stated
are preserved. An unknown origin is NEVER guessed — guessing fabricates authority, which is
the exact inversion the tier model exists to prevent. Half of this file exists to pin the
NEGATIVE arm, because that is the half that would be catastrophic to get wrong.

NOT A B→A PROMOTION PATH. There is deliberately no new promotion mechanism here: promotion
by REPETITION would let the engine promote its own inferred content to sacred. The fact was
never legitimately B — it was an A misfiled by a router that had lost the turn's origin.

PURE tests — no DB, no network, no LLM. The HTTP seams are fakes.
(Row-level end-to-end proof against the live stack: DEV/review/_restated_provenance_proof.py)
"""
import pytest

import src.re_embedder.embedder as E


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.owner.executed.append((sql, params))

    def fetchall(self):
        return self.owner.rows

    def fetchone(self):
        return (0,)

    @property
    def rowcount(self):
        return 0


class _FakeConn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _row(row_id, source):
    """(id, raw_text, source_ref, intent, created_at, source) — matches the drain SELECT."""
    return (row_id, "Tobias works for Northwind Logistics.", None, "STATEMENT", None, source)


class _Resp:
    """An extractor response carrying a spine-stamped user_stated edge.

    Shaped like the REAL /extract/rewrite success body (status="success" — the only
    status that endpoint returns on success). The re-mine gates on the POSITIVE
    success set, so a status-less fake would be rejected exactly like a real failure
    body, and these tests would assert nothing."""
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "success",
                "edges": [{"subject": "tobias", "rel_type": "works_for",
                           "object": "northwind logistics",
                           "fact_provenance": "user_stated"}]}


class _IngestResp:
    """The REAL /ingest success shape: status=\"valid\" is the ONLY success status
    /ingest returns (failures answer 200 with status=\"error\"/\"ingest_disabled\")."""
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "valid", "committed": 1, "staged": 0, "scalar_committed": 0,
                "entities": [], "facts": []}


def _capture_ingest(monkeypatch):
    """Record the `source` every /ingest POST is made under — the wire-level truth."""
    seen = []

    def _post(url, **kw):
        if url.endswith("/ingest"):
            seen.append(kw.get("json", {}).get("source"))
            return _IngestResp()
        return _Resp()

    monkeypatch.setattr(E.httpx, "post", _post)
    return seen


# ── 1. THE ORIGIN-LANE MAP ────────────────────────────────────────────────────

@pytest.mark.parametrize("origin,expected", [
    ("mcp", "mcp"),            # chat turn — live path ingests as source="mcp" → user_stated
    # document chunk — TIER-PRESERVING since 2026-08-21 (owner ruling): the live lane
    # ingests source="document" (machine-extracted → llm_inferred, staged B), so the
    # re-mine reproduces that tier instead of re-elevating to user_stated/Class A.
    ("document", "document"),
    ("MCP", "mcp"),            # case-insensitive
    ("  mcp  ", "mcp"),        # whitespace-tolerant
])
def test_user_stated_origins_resolve_to_their_live_ingest_source(monkeypatch, origin, expected):
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    assert E._reextract_ingest_source(origin) == expected


@pytest.mark.parametrize("origin", [
    None,                      # origin unrecorded
    "",                        # origin blank
    "some_future_writer",      # a lane nobody taught this map about
    123,                       # not even a string
])
def test_unknown_or_non_user_stated_origin_is_never_elevated(monkeypatch, origin):
    """FAIL TOWARD NOT ELEVATING. An origin we cannot establish keeps the legacy lane."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    assert E._reextract_ingest_source(origin) is None


def test_class_c_residue_origin_is_down_routed_not_elevated(monkeypatch):
    """'store_context_deferred' (Class-C residue) is NOT left on the legacy 'reextract'
    lane and NOT elevated either — it is DOWN-ROUTED to the explicit 'unattested' lane
    (authorship-honesty, 2026-08-15), where /ingest's provenance router forces
    llm_inferred and the class force lands staged B. Never A, never the C 30-day clock.

    This expectation was stale (it asserted None, the pre-down-route behaviour) and was
    RED at HEAD before the positive-success gates landed — the map changed on 2026-08-15
    without this file. It now pins the shipped behaviour."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    assert E._reextract_ingest_source("store_context_deferred") == "unattested"
    assert E._reextract_ingest_source("unattested") == "unattested"


# ── 2. FLAG OFF IS BYTE-IDENTICAL LEGACY ──────────────────────────────────────

@pytest.mark.parametrize("origin", ["mcp", "document", "store_context_deferred", None])
def test_flag_off_sends_every_origin_down_the_legacy_lane(monkeypatch, origin):
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", False)
    assert E._reextract_ingest_source(origin) is None


def test_flag_off_ingests_a_user_stated_turn_as_reextract(monkeypatch):
    """Flag OFF reproduces the DEFECT exactly — that is what 'byte-for-byte legacy' means."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", False)
    seen = _capture_ingest(monkeypatch)
    E.reextract_episodic(_FakeConn(rows=[_row(1, "mcp")]), "http://backend",
                         "u" * 36, None, batch_size=5, statement_route="rewrite")
    assert seen == ["reextract"]


# ── 3. EDGE STAMPING — the force applies ONLY to the not-elevatable lane ──────

def test_edges_are_left_unstamped_when_provenance_is_preserved(monkeypatch):
    """We do NOT stamp user_stated ourselves — /ingest's router owns that decision, here as
    on the live path, so there is no second copy of the provenance rule to drift."""
    monkeypatch.setattr(E.httpx, "post", lambda *a, **k: _Resp())
    edges = E._reextract_row_edges("t", "u" * 36, "http://backend", "rewrite",
                                   preserve_source="mcp")
    assert edges and edges[0]["fact_provenance"] == "user_stated"


def test_edges_are_forced_down_when_provenance_is_not_preserved(monkeypatch):
    """The safety half: a spine edge arrives stamped user_stated and the router PRESERVES a
    carried provenance, so without this force an unknown-origin row would sneak into A."""
    monkeypatch.setattr(E.httpx, "post", lambda *a, **k: _Resp())
    edges = E._reextract_row_edges("t", "u" * 36, "http://backend", "rewrite",
                                   preserve_source=None)
    assert edges and edges[0]["fact_provenance"] == "llm_inferred"


# ── 4. THE DRAIN WIRES IT THROUGH (both arms, on the real function) ──────────

def test_drain_reingests_a_user_stated_turn_under_its_original_source(monkeypatch):
    """POSITIVE ARM: origin 'mcp' → /ingest source="mcp" → router → user_stated → Class A."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    seen = _capture_ingest(monkeypatch)
    E.reextract_episodic(_FakeConn(rows=[_row(1, "mcp")]), "http://backend",
                         "u" * 36, None, batch_size=5, statement_route="rewrite")
    assert seen == ["mcp"]


def test_drain_holds_non_user_stated_origin_on_the_legacy_lane(monkeypatch):
    """NEGATIVE ARM: Class-C residue must NOT be promoted by being re-read. The shipped
    map DOWN-ROUTES it to the explicit 'unattested' lane (router forces llm_inferred,
    class lands staged B) — held down, never elevated to user_stated."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    seen = _capture_ingest(monkeypatch)
    E.reextract_episodic(_FakeConn(rows=[_row(1, "store_context_deferred")]),
                         "http://backend", "u" * 36, None,
                         batch_size=5, statement_route="rewrite")
    assert seen == ["unattested"]


def test_drain_holds_unknown_origin_on_the_legacy_lane(monkeypatch):
    """A row whose origin was never recorded is left at its lower tier, never guessed."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    seen = _capture_ingest(monkeypatch)
    E.reextract_episodic(_FakeConn(rows=[_row(1, None)]), "http://backend",
                         "u" * 36, None, batch_size=5, statement_route="rewrite")
    assert seen == ["reextract"]


def test_mixed_batch_routes_each_row_by_its_own_origin(monkeypatch):
    """Per-ROW decision, not per-batch: one preserved row must not drag the others up,
    and one residue row must not drag the user's turn down."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    seen = _capture_ingest(monkeypatch)
    rows = [_row(1, "mcp"), _row(2, "store_context_deferred"),
            _row(3, None), _row(4, "document")]
    E.reextract_episodic(_FakeConn(rows=rows), "http://backend", "u" * 36, None,
                         batch_size=10, statement_route="rewrite")
    # store_context_deferred takes the DOWN-ROUTED 'unattested' lane (held at
    # llm_inferred/staged B), NULL stays on the legacy 'reextract' lane, and a document
    # origin re-mines under its OWN tier (document → staged B, 2026-08-21 ruling).
    assert seen == ["mcp", "unattested", "reextract", "document"]


# ── 5. THE SELECT MUST ACTUALLY FETCH THE ORIGIN ─────────────────────────────

def test_drain_select_fetches_the_source_column(monkeypatch):
    """VERIFY THE READ: the whole fix is inert if the origin never leaves the database."""
    monkeypatch.setattr(E, "REEXTRACT_PRESERVE_PROVENANCE", True)
    _capture_ingest(monkeypatch)
    conn = _FakeConn(rows=[_row(1, "mcp")])
    E.reextract_episodic(conn, "http://backend", "u" * 36, None,
                         batch_size=5, statement_route="rewrite")
    selects = [s for s, _ in conn.executed if "FROM episodic_log" in s and "SELECT" in s]
    assert selects, "no SELECT against episodic_log was issued"
    assert "created_at, source" in selects[0], selects[0]
