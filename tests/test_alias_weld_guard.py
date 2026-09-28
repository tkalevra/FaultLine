"""ARRIVAL WELD GUARD — the contract, pinned.

A WELD is an ADDITIONAL, different surface added to an entity that already carries labels. Per
W3C SKOS Reference §5 that asserts the two surfaces name the SAME referent, so a wrong weld
rewrites what the user's word denotes. THE HARD LINE says a NAME (a memory) never becomes a
PLACE (an L4 type node) and vice versa.

Every example below is deliberately generic — no tenant data, no personal names — because the
guard is subject-agnostic and a test that only works for one domain would be evidence of the
opposite. See the internal design record for the production measurement.

⚠️ SCOPE OF THIS FILE — READ BEFORE TRUSTING A GREEN RUN. The cursor here is a MOCK that answers
the place probe from a boolean, so `_entity_is_user_asserted_place`'s SQL NEVER EXECUTES. A green
run here therefore proves NOTHING about the probe: an earlier revision passed 14/14 with the P31
arm inverted (`object_id` -> `subject_id`), which would have made every named instance a "place"
— the exact HARD-LINE violation the module exists to prevent. The probe, the evidence-authority
filter and the seat-anchor exemption are pinned in `tests/test_alias_weld_guard_db.py` against a
REAL PostgreSQL, and proven by mutation. Both files are the contract; neither is sufficient alone.
"""
import pytest

from src.entity_registry import weld_guard


class FakeCursor:
    """Minimal cursor: answers the guard's two probes, records savepoint discipline.

    ``labels``  -> rows returned for the entity_aliases probe.
    ``is_place`` -> whether the RDFS class-side probe finds a row.
    ``fail_on`` -> substring of a statement that should raise, to exercise fail-open.
    """

    def __init__(self, labels=(), is_place=False, fail_on=None):
        self.labels = list(labels)
        self.is_place = is_place
        self.fail_on = fail_on
        self.statements = []
        self._result = None

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("probe blew up")
        if "FROM entity_aliases" in sql:
            self._result = list(self.labels)
        elif "wikidata_pid" in sql:
            self._result = [(1,)] if self.is_place else []
        else:
            self._result = []

    def fetchall(self):
        return self._result or []

    def fetchone(self):
        return (self._result or [None])[0] if self._result else None


# ── ARM 1: a PLACE takes no MEMORY label ────────────────────────────────────────────────────

def test_a_user_surface_is_refused_on_a_node_that_is_a_class():
    """RDFS §3.3/§3.4: object of rdf:type / subject of rdfs:subClassOf == a CLASS.

    A user-supplied label on a class asserts `name == class` — the HARD-LINE category error
    that put a named instance's surface onto its own type node in production.
    """
    cur = FakeCursor(labels=[("widget", "inferred")], is_place=True)
    allow, arm = weld_guard.weld_verdict(cur, "e1", "zephyr", "user_stated")
    assert allow is False
    assert arm == weld_guard.ARM_PLACE


def test_an_engine_synonym_is_still_allowed_on_a_class():
    """The canonicalization lane must stay OPEN: engine forms on a place are the design."""
    cur = FakeCursor(labels=[("widget", "inferred")], is_place=True)
    allow, arm = weld_guard.weld_verdict(cur, "e1", "widget device", "inferred")
    assert allow is True
    assert arm == weld_guard.ALLOW


# ── ARM 2: a MEMORY label onto a node whose every label was authored by growth ───────────────

def test_a_growth_only_node_that_is_NOT_a_place_is_allowed_by_default():
    """CONTRACT NARROWED BY REVIEW — this test previously asserted the opposite.

    ARM 2 used to refuse here on "every existing label is growth" ALONE, with no place
    condition. Measured on production that refuses a new user-stated label on 2 526 entities,
    2 133 of which are not class-side by any provenance — INSTANCE nodes, where adding a
    surface is legitimate indexing, and the population includes `{configuration}+{config}` and
    `{transportation management}+{tms}`.

    ARM 2 is now OFF by default (and place-gated when armed), so a growth-only node that is
    NOT a place admits the label. What still refuses is ARM 1: a node the USER declared a
    class — covered by `test_enforce_is_the_default_and_a_refusal_skips_the_write`.
    """
    cur = FakeCursor(labels=[("widget", "inferred")], is_place=False)
    allow, arm = weld_guard.weld_verdict(cur, "e1", "zephyr", "user_stated")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_a_user_surface_is_allowed_on_a_node_whose_origin_carries_no_authority_claim():
    """THE ZERO-FALSE-REFUSAL CASE — this is the test that matters most.

    ``EntityRegistry.resolve()`` mints an entity's ORIGIN label with no source parameter, so it
    lands at the column default (rank at/below the provisioning floor) = "no claim recorded",
    NOT a growth claim. Every legitimate multi-alias entity measured on production has exactly
    this shape: an origin label with no recorded authority, plus user-stated additions. If this
    test goes red the guard has started eating real aliasing and MUST NOT ship.
    """
    cur = FakeCursor(labels=[("origin surface", "unspecified")], is_place=False)
    allow, arm = weld_guard.weld_verdict(cur, "e1", "another user surface", "user_stated")
    assert allow is True
    assert arm == weld_guard.ALLOW


def test_a_second_user_surface_is_allowed_once_a_user_surface_is_present():
    """Once the user has authored ANY label, the set is no longer growth-only."""
    cur = FakeCursor(labels=[("widget", "inferred"), ("zephyr", "user_stated")], is_place=False)
    allow, _ = weld_guard.weld_verdict(cur, "e1", "zeph", "user_stated")
    assert allow is True


# ── Non-welds ───────────────────────────────────────────────────────────────────────────────

def test_the_first_label_on_an_entity_is_never_a_weld():
    cur = FakeCursor(labels=[], is_place=True)
    allow, arm = weld_guard.weld_verdict(cur, "e1", "zephyr", "user_stated")
    assert allow is True
    assert arm == weld_guard.ALLOW


def test_re_registering_a_label_the_entity_already_holds_is_never_a_weld():
    """Re-ingest of the same turn must stay idempotent, not start failing."""
    cur = FakeCursor(labels=[("zephyr", "user_stated")], is_place=True)
    allow, arm = weld_guard.weld_verdict(cur, "e1", "zephyr", "user_stated")
    assert allow is True
    assert arm == weld_guard.ALLOW


# ── Fail-safe ───────────────────────────────────────────────────────────────────────────────

def test_a_failing_label_probe_fails_open():
    """A guard error must never block a write, and never poison the caller's transaction."""
    cur = FakeCursor(labels=[("widget", "inferred")], fail_on="FROM entity_aliases")
    allow, arm = weld_guard.weld_verdict(cur, "e1", "zephyr", "user_stated")
    assert allow is True
    assert arm == weld_guard.ALLOW
    assert any("ROLLBACK TO SAVEPOINT" in s for s in cur.statements)


def test_a_failing_place_probe_fails_open():
    cur = FakeCursor(labels=[("widget", "unspecified")], fail_on="wikidata_pid")
    allow, arm = weld_guard.weld_verdict(cur, "e1", "zephyr", "user_stated")
    assert allow is True
    assert arm == weld_guard.ALLOW


# ── Mode gating ─────────────────────────────────────────────────────────────────────────────

def test_enforce_is_the_default_and_a_refusal_skips_the_write(monkeypatch):
    """DEFAULT FLIPPED observe -> enforce. This pin is the contract, so it moved deliberately.

    The flip was gated on a READ-ONLY replay across all 12 production tenants: 313 real welds,
    10 refusals, every one a NAME welded onto a type node, ZERO false refusals on the seat
    anchor and zero on legitimate multi-alias entities, with the lexical lane proven open by a
    positive control. See `_DEFAULT_MODE` and the internal design record

    `observe` is still reachable via the env var and still writes; that is `test_observe_*`.
    """
    monkeypatch.delenv("ALIAS_WELD_GUARD", raising=False)
    assert weld_guard.guard_mode() == "enforce"
    cur = FakeCursor(labels=[("widget", "inferred")], is_place=True)
    assert weld_guard.refuse_weld(cur, "e1", "zephyr", "user_stated") is True


def test_observe_still_logs_without_changing_behaviour(monkeypatch):
    """The rollback lever. ALIAS_WELD_GUARD=observe restores byte-identical pre-guard writes."""
    monkeypatch.setenv("ALIAS_WELD_GUARD", "observe")
    cur = FakeCursor(labels=[("widget", "inferred")], is_place=True)
    assert weld_guard.refuse_weld(cur, "e1", "zephyr", "user_stated") is False


def test_enforce_refuses(monkeypatch):
    monkeypatch.setenv("ALIAS_WELD_GUARD", "enforce")
    cur = FakeCursor(labels=[("widget", "inferred")], is_place=True)
    assert weld_guard.refuse_weld(cur, "e1", "zephyr", "user_stated") is True


def test_off_runs_no_probe_at_all(monkeypatch):
    monkeypatch.setenv("ALIAS_WELD_GUARD", "off")
    cur = FakeCursor(labels=[("widget", "inferred")], is_place=True)
    assert weld_guard.refuse_weld(cur, "e1", "zephyr", "user_stated") is False
    assert cur.statements == []


def test_an_unknown_mode_falls_back_to_the_default(monkeypatch):
    """An unparseable value must resolve to the DEFAULT, not to a silently weaker mode."""
    monkeypatch.setenv("ALIAS_WELD_GUARD", "banana")
    assert weld_guard.guard_mode() == weld_guard._DEFAULT_MODE == "enforce"


def test_arm3_is_off_by_default_and_env_armable(monkeypatch):
    """ARM 3 is deliberately NOT part of the enforced default — it did not reach zero false
    refusals (one measured legitimate case: an acronym). Separate switch, separate decision."""
    monkeypatch.delenv("ALIAS_WELD_GUARD_ARM3", raising=False)
    assert weld_guard.arm3_enabled() is False
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "on")
    assert weld_guard.arm3_enabled() is True


# ── Subject-agnosticism, asserted structurally ──────────────────────────────────────────────

def test_the_guard_module_contains_no_domain_vocabulary():
    """Constraint 15's real gate: grep the shipped module for hardcoded rel/type/name literals.

    The only quoted identifiers permitted are provenance TIER names (a closed engine enum
    already owned by registry._PREFERENCE_RANK) and the two Wikidata RDFS-role property ids.
    """
    import inspect
    src = inspect.getsource(weld_guard)
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#")
    )
    # No rel-name equality checks anywhere in the module.
    assert "rel_type ==" not in code
    assert "rel_type in (" not in code
    # The RDFS roles are resolved from metadata, never by rel name.
    assert "wikidata_pid" in code
    for banned in ("instance_of", "subclass_of", "also_known_as", "pref_name", "has_pet"):
        assert f'"{banned}"' not in code, f"rel name literal {banned!r} leaked into the guard"
        assert f"'{banned}'" not in code, f"rel name literal {banned!r} leaked into the guard"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
