"""ARRIVAL WELD GUARD — the two write paths that BYPASS ``EntityRegistry.register_alias``.

WHY THESE ARE STRUCTURAL PINS AND NOT BEHAVIOURAL ONES. Both writers live deep inside very
large routines (`/ingest`'s post-commit alias sync, and `resolve_name_conflicts`'s LLM-arbitrated
entity merge). Driving either end-to-end would need an LLM stub and most of a tenant, and a test
built on that much scaffolding tends to pass because the scaffolding agrees with it — the exact
"mock that short-circuits the statement under test" failure this module has already been bitten
by once.

What actually matters on these two paths is an ORDERING and a PAYLOAD invariant, and both are
checkable directly against the source:

  * the guard must run BEFORE the write it guards (a guard placed after the mutation is
    decoration), and
  * the raw INSERT must carry `preference_source` (without it every row it makes records no
    authority claim, both memory-authority arms are structurally unable to fire, and guarding
    that path is inert no matter what it is told — which is exactly the state this work found).

These pins go red if either property is removed, which is what a mutation test needs. The
DECISION each path makes is covered separately, against a real database, in
`tests/test_alias_weld_guard_db.py`.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "src" / "api" / "main.py"
EMBEDDER = ROOT / "src" / "re_embedder" / "embedder.py"


def _function_source(path, name):
    """Return the source text of the named top-level (or nested) function."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} not found in {path}")


def _index(hay, needle, what):
    i = hay.find(needle)
    assert i != -1, f"{what}: expected to find {needle!r}"
    return i


def _function_node(path, name):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _gating_if_nodes(func):
    """Every ``if`` whose test IS EXACTLY a call to ``weld_guard.refuse_weld(...)``.

    Deliberately requires the call to BE the whole condition rather than merely appear inside
    it. Asserting only "the text appears before the write" is too weak: `if False and
    weld_guard.refuse_weld(...)` keeps the text in place, keeps the ordering, and disables the
    guard completely — a mutation run caught exactly that slipping through an earlier version
    of these pins.
    """
    out = []
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)
                and test.func.attr == "refuse_weld"):
            out.append(node)
    return out


def _first_line_containing(path, func, needle):
    """Line number of the first source line inside ``func`` containing ``needle``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for n in range(func.lineno, (func.end_lineno or len(lines)) + 1):
        if needle in lines[n - 1]:
            return n
    raise AssertionError(f"{needle!r} not found inside {func.name}")


# ── PATH 8 in the write-path audit: /ingest's raw also_known_as / pref_name alias sync ──────

def test_the_raw_alias_sync_records_provenance():
    """Without a `preference_source` the row lands at the column default, recording NO authority
    claim — under which neither memory-authority arm can fire. This is why guarding this path
    was INERT before the provenance was threaded through it."""
    src = _function_source(MAIN, "_ingest_impl")
    start = _index(src, "Sync is_preferred to entity_aliases", "the alias-sync block")
    block = src[start:start + 6000]
    insert = _index(block, "INSERT INTO entity_aliases", "the raw alias INSERT")
    # Only the COLUMN LIST, not the whole statement. A window wide enough to include the
    # ON CONFLICT clause would pass on the strength of the word appearing there, which a
    # mutation run proved: deleting the column from the INSERT left this pin green.
    stmt = block[insert:]
    collist = stmt[:_index(stmt, "VALUES", "the INSERT's VALUES keyword")]
    assert "preference_source" in collist, (
        "the raw alias sync must INSERT preference_source; without it every row it creates "
        "records no authority claim, both memory-authority arms are structurally unable to "
        "fire, and guarding this path is inert. Column list was: " + collist)


def test_the_raw_alias_sync_threads_a_REAL_provenance_value():
    """POSITION IS NOT EFFECT — and the column name alone is not a value.

    A review mutation replaced the `_alias_pref_source` lookup with the literal
    `_sync_src = 'unspecified'`. The column is still in the INSERT, the guard still runs, the
    ordering pin still passes — and the write-path fix is silently reverted, because
    'unspecified' records NO authority claim and both memory arms become unable to fire. So
    this pins the VALUE's provenance, not the column's presence: whatever is bound to
    `preference_source` here must be derived from the map the registry lane populated.
    """
    func = _function_node(MAIN, "_ingest_impl")
    assigns = [n for n in ast.walk(func)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "_sync_src" for t in n.targets)]
    assert assigns, "no `_sync_src` assignment found on the alias-sync path"
    for a in assigns:
        names = {n.id for n in ast.walk(a.value) if isinstance(n, ast.Name)}
        assert "_alias_pref_source" in names, (
            "_sync_src must be derived from _alias_pref_source (the provenance the registry "
            "lane already derived for this same alias). A hardcoded literal here reverts the "
            "write-path fix while leaving every structural pin green.")


def test_the_raw_alias_sync_is_guarded_before_it_writes():
    """A guard after the INSERT would not stop the write. It must precede it — otherwise
    enforcement at the registry is defeated on this lane: the registry declines the
    co-reference and this INSERT re-asserts it moments later in the same request."""
    func = _function_node(MAIN, "_ingest_impl")
    gates = _gating_if_nodes(func)
    assert gates, ("no `if weld_guard.refuse_weld(...)` gate found in /ingest — either the "
                   "guard was removed or its result no longer decides anything")
    insert_line = _first_line_containing(MAIN, func, "INSERT INTO entity_aliases")
    gate = min(gates, key=lambda n: n.lineno)
    assert gate.lineno < insert_line, (
        "the weld guard must run BEFORE the raw alias INSERT it guards")
    # ...and its body must actually SKIP the write.
    assert any(isinstance(n, ast.Continue) for n in ast.walk(gate)), (
        "a refusal on this path must `continue` past the INSERT; a gate whose body does not "
        "skip the write is decoration")


def test_the_raw_alias_sync_never_downgrades_a_recorded_provenance():
    """This writer's fallback value is the column default, which means 'no claim recorded'.
    On conflict it must therefore not overwrite a provenance another path already recorded —
    same monotone-observation rule the display_form COALESCE beside it follows."""
    src = _function_source(MAIN, "_ingest_impl")
    start = _index(src, "Sync is_preferred to entity_aliases", "the alias-sync block")
    block = src[start:start + 6000]
    insert = _index(block, "INSERT INTO entity_aliases", "the raw alias INSERT")
    stmt = block[insert:insert + 700]
    assert "WHEN EXCLUDED.preference_source = 'unspecified'" in stmt, (
        "the ON CONFLICT branch must keep the incumbent provenance when this path has none")


# ── PATH 14 in the write-path audit: the LLM-arbitrated entity merge ────────────────────────

def test_the_entity_merge_is_guarded_before_it_moves_anything():
    """The merge re-parents EVERY loser alias with raw UPDATEs, so none of them pass the
    registry guard. The pre-flight must precede STEP 1 — once facts have been repointed, a
    refusal can only produce a half-merged state, which is worse than either outcome."""
    func = _function_node(EMBEDDER, "resolve_name_conflicts")
    gates = _gating_if_nodes(func)
    assert gates, ("no `if weld_guard.refuse_weld(...)` gate found in the merge — either the "
                   "pre-flight was removed or its result no longer decides anything")
    gate = min(gates, key=lambda n: n.lineno)
    first_mutation = _first_line_containing(EMBEDDER, func, "UPDATE facts SET subject_id")
    alias_move = _first_line_containing(EMBEDDER, func, "UPDATE entity_aliases SET entity_id")
    assert gate.lineno < first_mutation, (
        "the weld pre-flight must run BEFORE the merge mutates anything — after step 1 a "
        "refusal can only produce a half-merged state")
    assert gate.lineno < alias_move


def test_an_inadmissible_merge_aborts_rather_than_half_applying():
    """Merge-level verdict, by design: a merge is ONE identity claim. Skipping just the
    offending alias would leave the loser an empty husk holding a name whose facts now live on
    the winner."""
    src = _function_source(EMBEDDER, "resolve_name_conflicts")
    assert "raise _MergeRefused(" in src, "an inadmissible surface must abort the whole merge"


def test_a_refused_merge_is_not_reported_as_a_merge_failure():
    """A deliberate refusal must have its own handler, ahead of the generic one, or it is
    logged as `entity_merge_failed` and reads as a bug in the merge."""
    src = _function_source(EMBEDDER, "resolve_name_conflicts")
    refused = _index(src, "except _MergeRefused", "the dedicated refusal handler")
    generic = _index(src, "except Exception as _merge_err", "the generic merge handler")
    assert refused < generic, (
        "the _MergeRefused handler must precede the generic one or it never runs")


def test_the_merge_preflight_fails_open():
    """The guard must never be able to BLOCK a merge through its own error — same fail
    direction as every other probe in the module."""
    src = _function_source(EMBEDDER, "resolve_name_conflicts")
    start = _index(src, "PRE-FLIGHT ADMISSIBILITY", "the pre-flight block")
    block = src[start:start + 3000]
    assert "merge_weld_preflight_failed" in block
    assert "_weld_block = None" in block


# ── the lexical lane must record its warrant on the branch production actually runs ─────────

def test_both_canonicalization_branches_record_the_lexical_warrant():
    """THE WRONG-SEAM TRAP. `CANONICALIZE_AT_CAPTURE` defaults OFF, so the INLINE branch is the
    one production runs. Fixing only the helper would leave the live lane writing `inferred`,
    and ARM 3 would then refuse the very lane the warrant exists to protect."""
    src = MAIN.read_text(encoding="utf-8")
    start = _index(src, 'CANONICALIZE_AT_CAPTURE', "the canonicalization flag branch")
    block = src[start:start + 800]
    assert block.count('preference_source="lexical"') == 2, (
        "BOTH the helper branch and the inline branch must record the lexical warrant")
    assert 'preference_source="inferred"' not in block


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
