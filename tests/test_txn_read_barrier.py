"""TX3 — the CLASS pin for `txn-held-across-batch`.

THE DEFECT THIS GUARDS
----------------------
A background lane does ``cur.execute(SELECT); cur.fetchall()`` and then loops making
LLM/HTTP calls **on the same connection**. psycopg2 opened the transaction on that first
statement and holds it until an explicit commit/rollback, so the whole batch runs
``idle in transaction``. With ``idle_in_transaction_session_timeout = 1min`` (pre-prod)
Postgres KILLS the connection mid-batch; every later statement fails "connection already
closed" and the lane's own evidence ledger (document ``chunk_state``, ``chunks_failed``)
is never written — a long document is left stranded at ``status='processing'`` with an
empty ledger while its chunks demonstrably failed in the logs.

Measured on pre-prod at 491951d4, one 3h window: 49 ``terminating connection due to
idle-in-transaction timeout`` in Postgres, and app-side 285 "connection already closed",
141 ``rollback_failed``, 45 ``reextract_row_failed``, 29 ``per_user_promotion_failed``,
21 ``document_drain_error``. A ``pg_stat_activity`` sample caught the held transaction
directly: ``reextract_episodic``'s ``SELECT ... FROM episodic_log WHERE reextracted_at IS
NULL``, ``backend_xid`` NULL (pure read), at 59.1s.

WHY THIS TEST IS AN ENUMERATION AND NOT A LIST
----------------------------------------------
The predecessor campaign (`correction-idle-txn`) fixed exactly this shape at ONE call
site and the class stayed open in ten others. A hand-written list of lanes has nothing
keeping it true. This test DERIVES the lane set from the module every run — any function
that is handed a connection AND transitively reaches a network primitive must call the
ONE shared release helper, ``src.api.db_read.release_read_transaction``. Add a new lane
of that shape without the barrier and this goes red, naming it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_EMBEDDER = (pathlib.Path(__file__).resolve().parents[1]
             / "src" / "re_embedder" / "embedder.py")

# A connection handed in by the caller. These lanes do not own their connection's
# lifecycle, so they cannot rely on close() to end the transaction for them.
# A connection is recognised BY USE, not by the name of the variable holding it.
# WHY: the previous version matched a six-name whitelist of parameter names, so renaming an
# argument to `connection` made a lane vanish from the enumeration entirely (critic round 3).
# A thing you call .cursor()/.commit()/.rollback() on IS a connection whatever it is called.
_CONN_METHODS = ("cursor", "commit", "rollback")

# Ways a lane can COME BY a connection without being handed one.
_CONNECT_ATTRS = ("connect",)                    # psycopg2.connect(...) / pg.connect(...)
_CONNECT_FUNCS = ("get_postgres_connection",)    # the repo's connection factory

# The BLOCKING primitives. The barrier exists because of the WAIT, so this names the wait —
# never the fix. Widened after critic round 3, which showed a lane blocking on a `psql`
# subprocess was not even seen as blocking.
_NET_MODULES = ("requests", "httpx")
_NET_METHODS = ("post", "get", "put", "delete", "patch")
_NET_OBJ_HINTS = ("client", "session")
_NET_ATTRS = ("run_batch", "run_cpu")
_NET_FUNCS = (
    "call_llm_with_retry_sync", "call_llm_with_retry_async", "call_llm_no_retry_sync",
    "embed_text", "run_cpu", "run_batch",
)
# Process / IPC waits — a `psql` subprocess blocks exactly as hard as an LLM call. Matched as
# `<module>.<anything>` rather than by bare function name: an earlier cut matched bare "call",
# "run", "join" and "wait" and promptly classified `",".join(...)` and every `_doc_log_crit`
# as a network hop. The lesson is that a blocking-primitive list must be anchored to its
# module, or it stops measuring anything.
_NET_PROC_MODULES = ("subprocess", "socket")
# Named blocking operations, anchored to the object they are called on where that matters.
_NET_BLOCKING_ATTRS = ("blpop", "brpop", "brpoplpush", "wait_for")
_NET_SLEEPERS = {("time", "sleep"), ("asyncio", "sleep")}

# Statements that OPEN a read transaction, and the ones that END one.
_READ_ATTRS = ("execute", "executemany", "fetchall", "fetchone", "fetchmany", "cursor")
_ENDERS = ("release_read_transaction", "commit", "rollback",
           "_rollback_and_reapply_search_path")

_BARRIER = "release_read_transaction"


def _module(path: pathlib.Path = None) -> ast.Module:
    return ast.parse((path or _EMBEDDER).read_text(encoding="utf-8"))


def _top_level_functions(mod: ast.Module) -> dict:
    return {n.name: n
            for n in mod.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _referenced_names(node) -> set:
    """Every name CALLED or PASSED BY REFERENCE by these node(s).

    By-reference matters: `_ask_brain(_query_llm_full_chain, ...)` blocks just as hard as
    calling it, and a call-only scan does not see it.
    """
    nodes = node if isinstance(node, list) else [node]
    out = set()
    for n in nodes:
        for x in ast.walk(n):
            if isinstance(x, ast.Call):
                f = x.func
                if isinstance(f, ast.Name):
                    out.add(f.id)
                elif isinstance(f, ast.Attribute):
                    out.add(f.attr)
            elif isinstance(x, ast.Name):
                out.add(x.id)
    return out


def _calls_network_directly(node) -> bool:
    nodes = node if isinstance(node, list) else [node]
    for n in nodes:
        for x in ast.walk(n):
            if not isinstance(x, ast.Call):
                continue
            f = x.func
            if isinstance(f, ast.Attribute):
                base = f.value.id.lower() if isinstance(f.value, ast.Name) else ""
                if base in _NET_MODULES and f.attr in _NET_METHODS:
                    return True
                if any(h in base for h in _NET_OBJ_HINTS) and f.attr in _NET_METHODS:
                    return True
                if base in _NET_PROC_MODULES:
                    return True
                if (base, f.attr) in _NET_SLEEPERS:
                    return True
                if f.attr in _NET_ATTRS or f.attr in _NET_BLOCKING_ATTRS:
                    return True
            if isinstance(f, ast.Name) and f.id in _NET_FUNCS:
                return True
    return False


def _lanes_that_block(funcs: dict) -> set:
    reaching = {k for k, v in funcs.items() if _calls_network_directly(v)}
    changed = True
    while changed:
        changed = False
        for name, node in funcs.items():
            if name in reaching:
                continue
            if _referenced_names(node) & reaching:
                reaching.add(name)
                changed = True
    return reaching


def _holds_a_connection(node) -> bool:
    """Does this lane hold a Postgres connection — handed, self-opened, or from a factory?

    Recognised BY USE. Three ways a lane was previously invisible, all found by critics:
      * the parameter was named something outside a hardcoded whitelist;
      * the lane opened its own `psycopg2.connect(...)` (this hid `main.py::extract_rewrite`,
        the lane that caused every termination this campaign measured);
      * the lane got its connection from `get_postgres_connection()`, which is how the
        provisioning modules — including this campaign's own predecessor lane — obtain one;
      * the connection rode an OBJECT ATTRIBUTE (round 2, blindspot #3, critic-found):
        `registry.db_conn` — `main.py::classify_fact_3d` held its caller's transaction for a
        measured 30.2s while sitting in no inventory at all, because the name lived on the
        object instead of in a parameter.
    """
    params = {a.arg for a in node.args.args} | {a.arg for a in getattr(node.args, "kwonlyargs", [])}
    for x in ast.walk(node):
        if not isinstance(x, ast.Call):
            continue
        f = x.func
        if isinstance(f, ast.Attribute):
            if f.attr in _CONN_METHODS and isinstance(f.value, ast.Name) and f.value.id in params:
                return True          # handed one, whatever it is called
            # ATTRIBUTE-CARRIED: registry.db_conn.cursor() / gate.db.commit() — used exactly
            # like a named parameter, but invisible to the parameter check BY CONSTRUCTION.
            if f.attr in _CONN_METHODS and isinstance(f.value, ast.Attribute):
                return True          # <object>.<attr>.<conn-method>(...)
            if f.attr in _CONNECT_ATTRS:
                base = f.value.id if isinstance(f.value, ast.Name) else ""
                if "psycopg2" in base or base == "pg":
                    return True      # opens its own
        if isinstance(f, ast.Name) and f.id in _CONNECT_FUNCS:
            return True              # gets one from the factory
    return False


def _local_blocking_defs(lane, module_blocking: set) -> set:
    """Nested defs inside one lane that themselves block (a closure is invisible to a
    module-scope call graph — this is how `_ground_coinstance_specificity` hid `_chain_for`)."""
    nested = [d for d in ast.walk(lane)
              if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef)) and d is not lane]
    local = {d.name for d in nested
             if _calls_network_directly(d) or (_referenced_names(d) & module_blocking)}
    size = -1
    while len(local) != size:
        size = len(local)
        for d in nested:
            if d.name not in local and (_referenced_names(d) & (module_blocking | local)):
                local.add(d.name)
    return local


def _connection_lanes(path: pathlib.Path = None):
    """(name -> node) for every lane that holds a connection and can block."""
    funcs = _top_level_functions(_module(path))
    blocking = _lanes_that_block(funcs)
    return {n: funcs[n] for n in blocking if _holds_a_connection(funcs[n])}


def _is_connection_with(st) -> bool:
    """`with psycopg2.connect(...) as c:` — psycopg2 COMMITS on block exit.

    It does not CLOSE the connection (that is the leak `db_read.py` documents), but the
    transaction is over, so the statements after the block start clean.
    """
    if not isinstance(st, (ast.With, ast.AsyncWith)):
        return False
    for item in st.items:
        for x in ast.walk(item.context_expr):
            if (isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                    and x.func.attr in _CONNECT_ATTRS):
                base = x.func.value.id if isinstance(x.func.value, ast.Name) else ""
                if "psycopg2" in base or base == "pg":
                    return True
    return False


def _own_expressions(st):
    """A statement's OWN expressions, never its nested bodies."""
    if isinstance(st, (ast.If, ast.While)):
        return [st.test]
    if isinstance(st, (ast.For, ast.AsyncFor)):
        return [st.iter]
    if isinstance(st, (ast.With, ast.AsyncWith)):
        return [i.context_expr for i in st.items]
    if isinstance(st, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return []
    return [st]


def _leaks_a_read(node) -> bool:
    """Does this helper READ and then leave its transaction open on the SUCCESS path?

    This is the distinction that makes the re-arm check usable. `_climb_state_should_skip`
    reads and rolls back ONLY inside its `except` — so on the success path the caller gets a
    connection with a fresh open read, which is exactly how the live defect worked. A helper
    that commits, closes, or scopes its work in `with psycopg2.connect(...)` hands the caller
    a clean connection and must NOT re-arm the check, or the bar cries wolf and stops
    measuring anything.
    """
    if not _holds_a_connection(node):
        return False
    if not (_referenced_names(node) & set(_READ_ATTRS)):
        return False
    handlers = [h for x in ast.walk(node) if isinstance(x, ast.Try) for h in x.handlers]
    in_handler = set()
    for h in handlers:
        in_handler |= {id(y) for y in ast.walk(h)}
    for x in ast.walk(node):
        if id(x) in in_handler:
            continue                      # an `except`-only rollback does not clean the
        if isinstance(x, ast.Call):       # SUCCESS path
            f = x.func
            if isinstance(f, ast.Attribute) and f.attr in ("commit", "close"):
                return False
            if isinstance(f, ast.Name) and f.id == "release_read_transaction":
                return False
            if isinstance(f, ast.Attribute) and f.attr in _CONNECT_ATTRS:
                base = f.value.id if isinstance(f.value, ast.Name) else ""
                if "psycopg2" in base or base == "pg":
                    # a `with psycopg2.connect(...)` scope commits on exit
                    for y in ast.walk(node):
                        if isinstance(y, (ast.With, ast.AsyncWith)) and _is_connection_with(y):
                            return False
    return True


def _blocking_sites_on_an_open_read(path: pathlib.Path = None):
    """Blocking call sites reached while a read transaction is OPEN.

    THE RE-ARM, and why a latching walk is not enough. The previous version set
    "a barrier has run" once and never cleared it, so this passed:

        release_read_transaction(conn)          # barrier
        fp = _concept_fingerprint(conn)         # opens a FRESH read transaction
        _climb_state_should_skip(conn, fp)      # reads; rolls back only in `except`
        _ask_brain(_query_llm_full_chain, ...)  # BLOCKS with that read still open

    That is not hypothetical — it is `climb_classification_chains`, and on the deployed
    image Postgres killed its connection twice in three minutes (02:42:25 and 02:45:06),
    taking the whole `_ont_db` subsystem chain down with it four seconds later. A barrier is
    therefore CONSUMED by the next read, exactly as the transaction is.
    """
    funcs = _top_level_functions(_module(path))
    module_blocking = _lanes_that_block(funcs)
    readers = {k for k, v in funcs.items() if _leaks_a_read(v)}
    out = []
    for name, node in _connection_lanes(path).items():
        local = _local_blocking_defs(node, module_blocking)

        def walk(stmts, open_read):
            for st in stmts:
                own = _own_expressions(st)
                names = _referenced_names(own)
                if names & set(_ENDERS):
                    open_read = False
                blocks = (_calls_network_directly(own)
                          or bool(names & (module_blocking | local)))
                if blocks and open_read:
                    out.append((getattr(st, "lineno", 0), name))
                if (names & set(_READ_ATTRS)) or (names & readers):
                    open_read = True
                inner = open_read
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                        walk(sub, inner)
                for h in getattr(st, "handlers", []) or []:
                    walk(h.body, inner)
                if _is_connection_with(st):
                    open_read = False
            return open_read

        walk(node.body, False)
    return sorted(set(out))


_MAIN = (pathlib.Path(__file__).resolve().parents[1] / "src" / "api" / "main.py")


def _opens_its_own_connection(node) -> bool:
    """Kept as its own predicate because the main.py surface is frozen against it."""
    return _holds_a_connection(node)


def test_the_enumeration_is_not_empty():
    """A silent zero would make every bar below vacuously green."""
    lanes = _connection_lanes()
    assert len(lanes) >= 15, (
        f"only {len(lanes)} connection lanes found in embedder.py — the predicate has "
        "probably drifted from the module and these bars are no longer measuring anything")


def test_no_blocking_call_is_reached_with_an_open_read():
    """THE BAR. Not 'a barrier exists somewhere' — no blocking call may be reached while a
    read transaction is open, with the barrier CONSUMED by each subsequent read."""
    offenders = _blocking_sites_on_an_open_read()
    assert not offenders, (
        "these blocking call sites are reached with a read transaction still open, so the "
        "read rides across the wait and Postgres kills the connection at "
        "idle_in_transaction_session_timeout:\n"
        + "\n".join(f"    embedder.py::{n} — line {ln}" for ln, n in offenders)
        + f"\n\nPut {_BARRIER}(conn, context=...) IMMEDIATELY BEFORE the blocking call, "
          "not merely at the top of the enclosing block.")


# NOTE — a weaker "every lane names the helper somewhere" bar used to sit here and has been
# DELETED, not weakened. It is strictly subsumed by the order bar above (a lane with no
# blocking call on an open read needs no barrier at all), and it produced FALSE failures on
# the three lanes that legitimately commit-and-close before their hop
# (`fast_drain_pending_documents_prepass`, `_sweep_inverted_staged_hierarchy_rows`,
# `_standalone_doc_drain_loop`). A redundant bar that cries wolf trains people to ignore the
# one that matters.


def test_the_recovery_helper_commits_its_rebind():
    """`_rollback_and_reapply_search_path` must not leave a fresh transaction open.

    It is called from every `except` arm in the module. `SET search_path` is a statement like
    any other, so psycopg2 opens a transaction to run it — and every caller then goes straight
    back to work, often a network call. An uncommitted re-bind therefore put the connection
    back into `idle in transaction` for the whole of the next unit of work: the recovery
    helper was re-arming the failure it exists to recover from.
    """
    from src.re_embedder import embedder

    events = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            events.append(("execute", sql))

    class _Conn:
        def cursor(self):
            return _Cur()

        def rollback(self):
            events.append(("rollback", None))

        def commit(self):
            events.append(("commit", None))

    embedder._rollback_and_reapply_search_path(_Conn(), "faultline_pin_tenant")
    kinds = [k for k, _ in events]
    assert kinds[0] == "rollback", f"expected a rollback first, got {events}"
    assert any(k == "execute" and "search_path" in (v or "") for k, v in events), events
    assert "commit" in kinds, (
        "the re-applied search_path was left UNCOMMITTED — the connection is now idle in "
        f"transaction for the caller's next blocking call. events={events}")
    assert kinds.index("commit") > kinds.index("rollback")
    assert kinds.count("rollback") == 1, events


def test_the_stdlib_logger_is_never_fed_structlog_kwargs():
    """TX4 — a masking defect, not a cosmetic one.

    `embedder.py`'s `log` is a STDLIB logger. `log.warning("event", error=...)` raises
    `TypeError: Logger._log() got an unexpected keyword argument`. Sixteen such sites existed;
    two sat inside the per-user lifecycle block's `except` arms, so the TypeError escaped the
    handler and surfaced as `per_user_promotion_failed: Logger._log() got an unexpected
    keyword argument 'error'` — the real error never printed, the rest of that tenant's pass
    skipped.
    """
    mod = _module()
    allowed = {"extra", "exc_info", "stack_info", "stacklevel"}
    offenders = []
    for n in ast.walk(mod):
        if (isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "log"
                and n.func.attr in ("info", "warning", "error",
                                    "debug", "critical", "exception")):
            bad = [k.arg for k in n.keywords if k.arg not in allowed]
            if bad:
                offenders.append((n.lineno, n.func.attr, bad))
    assert not offenders, (
        "`log` in embedder.py is a STDLIB logger; these calls pass structlog kwargs and raise "
        "TypeError at runtime, masking whatever they were trying to report:\n"
        + "\n".join(f"    line {ln}: log.{lvl}(..., {kw})" for ln, lvl, kw in offenders))


@pytest.mark.parametrize("lane", [
    "reextract_episodic",
    "drain_pending_documents",
    "_drain_document_via_queue",
    "promote_staged_facts",
    "expire_staged_facts",
    "promote_class_c_hits",
    "decay_class_c_hits",
    "climb_classification_chains",
])
def test_named_wound_lanes_stay_covered(lane):
    """The lanes the wound was actually MEASURED on, pinned by name as well as by class.

    `climb_classification_chains` is here because Postgres killed its connection on the
    DEPLOYED image at 02:42:25 and 02:45:06 — it is a measured lane, not a theoretical one.
    """
    funcs = _top_level_functions(_module())
    assert lane in funcs, f"embedder.py::{lane} no longer exists — re-anchor this pin"
    assert _BARRIER in _referenced_names(funcs[lane]), (
        f"embedder.py::{lane} lost its read barrier")


# ─────────────────────────────────────────────────────────────────────────────────────
# THE OTHER HALF OF THE CLASS — src/api/main.py, RECORDED not fixed.
#
# WHY THIS SECTION USES A DIFFERENT PREDICATE FROM THE ONE ABOVE, AND SAYS SO.
# The bars above are a HARD GUARANTEE for the file this campaign owns: every blocking call
# site must have `release_read_transaction` on its path, checked in source order. That is
# precise because the lanes were written to satisfy it.
#
# `main.py` cannot be scored that way and it is dishonest to pretend otherwise. Its request
# handlers are thousands of lines of nested try/with/if, and a flow-INSENSITIVE walk cannot
# decide whether a `commit()` three blocks up dominates a network hop further down. I tried
# both tunings and neither is trustworthy: a strict walk called `purge_user_data` and three
# `embedder.py` lanes defective when reading the code shows they commit and close before
# their hop, and a permissive walk cleared `extract_rewrite` — the ONE lane this campaign's
# own PID attribution proves caused every termination it measured.
#
# So this half is deliberately NOT a defect list. It is the STRUCTURAL SURFACE: every
# top-level function in `main.py` that is handed a connection OR opens one itself AND can
# reach a blocking call. It over-reports by construction, and that is the point — it is a
# review queue for the child that owns `main.py`, and a ratchet that fails when the surface
# GROWS.
#
# THE MEASURED CULPRIT IS NAMED SEPARATELY BELOW so it can never be lost in the list.
# ─────────────────────────────────────────────────────────────────────────────────────

# The lane the evidence actually indicts. `main.py::extract_rewrite` opens its own
# connection (`db_gliner = psycopg2.connect(...)`), reads
# `SELECT DISTINCT unnest(member_entity_types) FROM entity_taxonomies` on it, and then
# blocks on `await run_cpu(...)` — the admission-capped background CPU pool — before
# closing it in a `finally`. Measured post-deploy on pre-prod: that read was held
# idle-in-transaction to 58.4-59.6s (backend_xid NULL, i.e. a pure read) and accounted for
# ALL TWELVE in-window Postgres terminations and 433 of 440 held-transaction samples. The
# sibling site in `main.py::extract` closes its connection immediately after `fetchall()`
# and so cannot idle, which is what leaves this one as the only possible holder.
#
# FIXED by the txn-mainpy-held-read child (this file's owner since that child): the taxonomy
# read is followed by release_read_transaction(db_gliner, ...) before the run_cpu hop, the
# strengthen phase commits its SET bind and each per-entity UPDATE. The lane keeps its own
# psycopg2.connect (unchanged churn) — the HOLD was the defect, not the work.
_MAIN_MEASURED_CULPRIT = "extract_rewrite"
# Lanes FIXED for this class: they may hold a connection and block, but the barrier
# (release_read_transaction on the path, or a commit ending the transaction before the hop)
# is pinned per-lane by test_extract_rewrite_never_holds_a_txn_across_a_hop below.
# They are EXEMPT from the ceiling, not deleted from the record.
_MAIN_FIXED_LANES = frozenset({"extract_rewrite"})

# The structural surface, frozen. A CEILING and a review queue — NOT a list of proven
# defects, and not a census of open ones.
_MAIN_CLASS_SURFACE = frozenset({
    "_apply_taxonomy_rules",
    "_attach_hearst_hyponyms",
    "_attach_to_seeded_backbone",
    "_attach_wordnet_hypernym_ladder",
    "_classify_value_subject_residue",
    "_commit_staged",
    "_ensure_tenant_ready",
    "_harvest_via_sentence_pipeline",
    "_immediate_sync_class_c_staged",
    "_llm_discover_taxonomy_from_facts",
    "_purge_conflicting_staged_hierarchy_facts",
    "_query_impl",
    "_retraction_intent_extraction_llm",
    "_retraction_semantic_fallback",
    "_seed_domain_operational_ontology",
    "_unified_correction_extraction_llm",
    "classify_intent",
    "correct_fact",
    "determine_path",
    "forget_fact",
    "get_confidence_gate",
    "ingest",
    "learn_topic",
    "lifespan",
    "query_negation_patterns",
    "retract_fact",
    "wait_for_schema_ready",
})


def _holds_a_connection_r1(node) -> bool:
    """The ROUND-1 predicate, kept verbatim for the widening pin: the widened one must
    see a SUPERSET, and the delta must be non-empty - a widening that names nothing new
    measured nothing, and one that names fewer is a regression."""
    params = {a.arg for a in node.args.args} | {a.arg for a in getattr(node.args, "kwonlyargs", [])}
    for x in ast.walk(node):
        if not isinstance(x, ast.Call):
            continue
        f = x.func
        if isinstance(f, ast.Attribute):
            if f.attr in _CONN_METHODS and isinstance(f.value, ast.Name) and f.value.id in params:
                return True
            if f.attr in _CONNECT_ATTRS:
                base = f.value.id if isinstance(f.value, ast.Name) else ""
                if "psycopg2" in base or base == "pg":
                    return True
        if isinstance(f, ast.Name) and f.id in _CONNECT_FUNCS:
            return True
    return False


def _class_surface_r1(path: pathlib.Path) -> set:
    """Class surface under the ROUND-1 predicate (no attribute-carried connections)."""
    funcs = _top_level_functions(_module(path))
    blocking = _lanes_that_block(funcs)
    return {n for n in blocking if _holds_a_connection_r1(funcs[n])}


def _opens_its_own_connection(node: ast.AST) -> bool:
    """A lane that calls ``psycopg2.connect(...)`` itself.

    THE BLINDSPOT THIS CLOSES. Until this existed, the enumeration asked only whether a
    connection arrived as an ARGUMENT — so a lane that opens its own was invisible BY
    CONSTRUCTION, and `main.py::extract_rewrite`, the single lane the post-deploy PID
    attribution indicts, sat in no inventory and under no ratchet.
    """
    for x in ast.walk(node):
        if (isinstance(x, ast.Call)
                and isinstance(x.func, ast.Attribute)
                and x.func.attr == "connect"):
            base = x.func.value.id if isinstance(x.func.value, ast.Name) else ""
            if "psycopg2" in base or base == "pg":
                return True
    return False


def _class_surface(path: pathlib.Path) -> set:
    """Every lane in `path` that holds a connection (handed OR self-opened) and can block."""
    funcs = _top_level_functions(_module(path))
    blocking = _lanes_that_block(funcs)
    return {name for name in blocking if _holds_a_connection(funcs[name])}


def test_the_measured_culprit_is_never_lost_from_the_record():
    """The one lane the evidence indicts must stay named, stay self-connecting, and stay FIXED.

    Updated by the txn-mainpy-held-read fix (this lane's owning child): the tripwire used to
    fire when the lane "disappears or stops opening its own connection" — it still does both
    jobs — and now ALSO fires if the barrier itself is lost, which is the revert-red pin for
    the fix.
    """
    funcs = _top_level_functions(_module(_MAIN))
    assert _MAIN_MEASURED_CULPRIT in funcs, (
        f"main.py::{_MAIN_MEASURED_CULPRIT} no longer exists — it was the measured cause of "
        "every Postgres termination this campaign observed post-deploy. If it was renamed, "
        "re-anchor this pin; do not delete it.")
    assert _MAIN_MEASURED_CULPRIT in _MAIN_FIXED_LANES
    assert _opens_its_own_connection(funcs[_MAIN_MEASURED_CULPRIT]), (
        f"main.py::{_MAIN_MEASURED_CULPRIT} no longer opens its own connection — re-measure "
        "before assuming the holding behaviour changed with it")
    assert _BARRIER in _referenced_names(funcs[_MAIN_MEASURED_CULPRIT]), (
        f"main.py::{_MAIN_MEASURED_CULPRIT} lost its read barrier "
        f"({_BARRIER}) — this is the fix for the 36/36 window terminations; re-land it")


def test_the_main_py_surface_is_recorded_not_forgotten():
    """Every recorded name must still exist — catches a rename or a removal."""
    funcs = _top_level_functions(_module(_MAIN))
    vanished = sorted(n for n in _MAIN_CLASS_SURFACE if n not in funcs)
    assert not vanished, (
        f"recorded main.py lanes no longer exist (renamed or removed): {vanished} — re-run "
        "the enumeration and update _MAIN_CLASS_SURFACE. Do not delete the list.")
    assert len(_MAIN_CLASS_SURFACE) >= 23, "the surface must not be emptied to make this green"
    # TM3 (txn-mainpy-held-read): fixed lanes LEAVE the ceiling. A fixed lane may never creep
    # back onto it — that would mean the barrier was lost again.
    assert not (_MAIN_CLASS_SURFACE & _MAIN_FIXED_LANES), (
        "these lanes were FIXED for the held-read class and must stay off the ceiling: "
        f"{sorted(_MAIN_CLASS_SURFACE & _MAIN_FIXED_LANES)}")


def _barriered_lanes(path: pathlib.Path) -> set:
    """Lanes in the class surface that reference the shared read barrier.

    A barriered lane has done the work the ratchet asks for, so it is exempt from the ceiling
    (TM3, txn-mainpy-held-read: fixed lanes LEAVE the list). The exemption is earned by the
    symbol on the lane's path, not by hand-maintained membership — a lane that loses the
    barrier re-enters the ceiling automatically and this test names it.
    """
    funcs = _top_level_functions(_module(path))
    return {n for n, node in funcs.items() if _BARRIER in _referenced_names(node)}


def test_no_new_lane_joins_the_main_py_class_surface():
    """RATCHET. The surface may shrink; it may not grow.

    Growth-only by design: a pin that also failed when someone REMOVED a lane would punish
    exactly the work this ratchet is asking for. Lanes carrying the shared barrier
    (release_read_transaction) are exempt — they have left the ceiling by fixing, and their
    per-lane hard bar lives in test_extract_rewrite_never_holds_a_txn_across_a_hop.
    """
    exempt = _MAIN_FIXED_LANES | _barriered_lanes(_MAIN)
    new = sorted(_class_surface(_MAIN) - _MAIN_CLASS_SURFACE - exempt)
    assert not new, (
        "NEW lanes in src/api/main.py hold a Postgres connection (handed or self-opened) and "
        "can reach a network/LLM call — the same class this campaign closed in the "
        "re_embedder, and measured live on this file at 58-59s holds:\n"
        + "\n".join(f"    main.py::{n}" for n in new)
        + "\n\nReview it against src.api.db_read.release_read_transaction, or add it to "
          "_MAIN_CLASS_SURFACE naming the child that owns it.")


def test_the_owned_file_has_no_self_opening_blind_spot():
    """The blindspot that hid extract_rewrite must not hide anything in the OWNED file.

    Every self-opening lane in embedder.py is checked by hand and recorded here with WHY it
    is safe, so this can never become an unexamined exemption.
    """
    funcs = _top_level_functions(_module(_EMBEDDER))
    lanes = _connection_lanes(_EMBEDDER)
    self_opening = sorted(n for n in lanes
                          if not any(a.arg in ("db_conn", "conn", "db", "admin_conn",
                                               "db_per_user", "_ont_db", "cur", "cursor")
                                     for a in funcs[n].args.args))
    reviewed = {
        # commits its probe, then delegates to the barriered drain_pending_documents
        "fast_drain_pending_documents_prepass",
        # commits before the Qdrant hop
        "_sweep_inverted_staged_hierarchy_rows",
        # its `with psycopg2.connect(...)` block exits (and commits) before the prepass call
        "_standalone_doc_drain_loop",
        # the sweep loop itself — carries the subsystem-boundary barriers
        "main",
    }
    unreviewed = sorted(set(self_opening) - reviewed)
    assert not unreviewed, (
        "these embedder.py lanes open their own connection and can block, and nobody has "
        f"reviewed them: {unreviewed} — read each one and either barrier it or record here "
        "WHY it is safe. An unexamined exemption is how extract_rewrite stayed invisible.")


# ─────────────────────────────────────────────────────────────────────────────────────
# txn-mainpy-held-read — the HARD BAR for the lane this campaign owns and FIXED.
#
# The ceiling above is deliberately structural (over-reporting). extract_rewrite is past
# that: it was the measured culprit (36/36 window terminations, PID-attributed to its
# taxonomy read held across 'await run_cpu'), it is fixed, and a fixed lane gets a precise
# per-lane bar. This is a statement-order STATE MACHINE, not a flow-insensitive scan — the
# cortex-corrected method: a READ sets open-read, an ENDER clears it, and a blocking call
# with an open transaction is the defect. Scoped to ONE function so it cannot cry wolf on
# the rest of main.py's 59k lines.
# ─────────────────────────────────────────────────────────────────────────────────────

_MAIN_BLOCKING_FUNCS = frozenset({
    "run_cpu", "run_batch",
    "call_llm_with_retry_sync", "call_llm_with_retry_async",
    "call_llm_no_retry_sync", "embed_text",
})
# Attr-blockers are ANCHORED to their object (asyncio.to_thread / asyncio.gather, or an
# HTTP module/client base). A bare attr match on _NET_METHODS classified dict.get(...) as a
# network hop on its first run — the same lesson the embedder half learned with ",".join(...).
_MAIN_BLOCKING_ATTRS = frozenset({"to_thread", "gather"})
_MAIN_NET_BASES = frozenset({"requests", "httpx", "client", "session"})
_MAIN_TXN_OPENERS = frozenset({"execute", "executemany", "fetchall", "fetchone", "fetchmany"})
_MAIN_TXN_ENDERS = frozenset({"commit", "rollback", "close"})


def _walk_statements_in_order(stmts):
    """Yield every statement in SOURCE order, descending into compound bodies exactly once
    (if/for/while/try/with) — ast.walk is BFS and cannot do this."""
    for st in stmts:
        yield st
        if isinstance(st, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.Try)):
            yield from _walk_statements_in_order(st.body)
            if isinstance(st, ast.Try):
                for h in st.handlers:
                    yield from _walk_statements_in_order(h.body)
            yield from _walk_statements_in_order(getattr(st, "orelse", []) or [])
            yield from _walk_statements_in_order(getattr(st, "finalbody", []) or [])
        elif isinstance(st, (ast.With, ast.AsyncWith, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _walk_statements_in_order(st.body)


def _lane_hold_violations(func) -> list:
    """(lineno, why) for every blocking call made while a transaction is open in func."""
    open_conns = set()          # names of connection objects with an open transaction
    cursor_owner = {}           # cursor name -> connection name (a cursor shares the txn)
    violations = []

    def _base_name(n):
        return n.id if isinstance(n, ast.Name) else None

    for st in _walk_statements_in_order(func.body):
        # `with <conn>.cursor() as cur:` — the alias lives in the withitem's optional_vars,
        # NOT inside the context expression, so capture it from the statement itself. A
        # missed alias opens the txn on the CURSOR name, which no conn.close() can clear.
        if isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                ce = item.context_expr
                if (isinstance(ce, ast.Call) and isinstance(ce.func, ast.Attribute)
                        and ce.func.attr == "cursor"):
                    cbase = _base_name(ce.func.value)
                    if cbase:
                        for tgt in [t for t in ast.walk(item.optional_vars)
                                    if isinstance(t, ast.Name)]:
                            cursor_owner[tgt.id] = cbase
        # ONLY the statement's OWN expressions: ast.walk on a compound statement visits its
        # nested calls in BFS order, which can score a run_cpu deep in the try body BEFORE
        # the commit in that same body — an ordering hallucination, not source order. The
        # bodies themselves are yielded separately by _walk_statements_in_order.
        exprs = _own_expressions(st)
        if not exprs:
            continue          # a Try/def has no own expressions — its body is yielded separately
        for e in exprs:
            for x in ast.walk(e):
                if not isinstance(x, ast.Call):
                    continue
                f = x.func
                # ── connection bookkeeping ────────────────────────────────────────
                if isinstance(f, ast.Attribute):
                    base = _base_name(f.value)
                    if f.attr == "cursor" and base is not None:
                        for tgt in [t.id for t in ast.walk(e)
                                    if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)]:
                            cursor_owner[tgt] = base
                    elif f.attr in _MAIN_TXN_OPENERS:
                        owner = cursor_owner.get(base, base)
                        if owner is not None:
                            open_conns.add(owner)
                    elif f.attr in _MAIN_TXN_ENDERS:
                        owner = cursor_owner.get(base, base)
                        open_conns.discard(owner)
                        if f.attr == "close":
                            cursor_owner = {k: v for k, v in cursor_owner.items() if v != owner}
                    elif (f.attr in _MAIN_BLOCKING_ATTRS
                          or (f.attr in _NET_METHODS and base in _MAIN_NET_BASES)):
                        if open_conns:
                            violations.append((x.lineno, f"blocking .{f.attr}(...) with open txn on {sorted(open_conns)}"))
                elif isinstance(f, ast.Name):
                    if f.id == _BARRIER:
                        for a in x.args:
                            n = _base_name(a)
                            if n:
                                open_conns.discard(cursor_owner.get(n, n))
                    elif f.id in _MAIN_BLOCKING_FUNCS and open_conns:
                        violations.append((x.lineno, f"blocking {f.id}(...) with open txn on {sorted(open_conns)}"))
    return violations


def test_extract_rewrite_never_holds_a_txn_across_a_hop():
    """The hard bar for the fixed lane: no blocking call with an open transaction.

    Revert-red by construction: remove release_read_transaction after the taxonomy read (or
    either strengthen commit) and this names the exact line of the re-opened hold — the same
    line pg_stat_activity caught at 58-59s in production.
    """
    funcs = _top_level_functions(_module(_MAIN))
    lane = funcs[_MAIN_MEASURED_CULPRIT]
    violations = _lane_hold_violations(lane)
    assert not violations, (
        "main.py::extract_rewrite holds a database transaction across a blocking call — "
        "the exact class that produced 36/36 window terminations:\n"
        + "\n".join(f"    line {ln}: {why}" for ln, why in violations))


def test_the_ratchet_names_a_synthetic_offender(tmp_path):
    """TM3 ablation: a NEW offending lane in a copy of main.py must be NAMED by the ratchet.

    Proves the growth-only test still measures after the fixed-lane exemption was added —
    an exemption that silenced the ratchet entirely would pass everything.
    """
    src = _MAIN.read_text(encoding="utf-8")
    src += (
        "\n\ndef _synthetic_held_read_offender(db):\n"
        "    with db.cursor() as cur:\n"
        "        cur.execute('SELECT 1')\n"
        "        rows = cur.fetchall()\n"
        "    return await run_cpu(_f, rows)\n"
    )
    copy = tmp_path / "main_synthetic.py"
    copy.write_text(src, encoding="utf-8")
    exempt = _MAIN_FIXED_LANES | _barriered_lanes(copy)
    new = sorted(_class_surface(copy) - _MAIN_CLASS_SURFACE - exempt)
    assert "_synthetic_held_read_offender" in new, (
        f"the ratchet failed to name an injected offending lane; new={new}")


def test_the_widened_predicate_sees_the_registry_pattern():
    """Round 2 (critic blindspot #3): attribute-carried connections can never hide again.

    The widening names no NEW blocking lane in main.py today (verified by enumeration) —
    but it DOES see main.py::classify_fact_3d, the lane that held a caller transaction for
    a measured 30.2s while sitting in no inventory, because its connection arrives as
    registry.db_conn. That lane is the witness the widening is non-vacuous: invisible to
    the round-1 predicate, visible to the widened one.
    """
    funcs = _top_level_functions(_module(_MAIN))
    lane = funcs.get("classify_fact_3d")
    assert lane is not None, "main.py::classify_fact_3d renamed/removed — re-anchor this pin"
    assert not _holds_a_connection_r1(lane), (
        "the round-1 predicate suddenly sees classify_fact_3d — the r1 snapshot above has "
        "drifted; re-freeze it from the commit that introduced the widening")
    assert _holds_a_connection(lane), (
        "the widened predicate no longer recognizes the registry.db_conn pattern — "
        "attribute-carried connections are hidden again (blindspot #3 reopened)")


def test_the_widening_never_shrinks_the_surface():
    """Growth-only discipline for the predicate itself: widened >= round-1, always."""
    r1 = _class_surface_r1(_MAIN)
    wide = _class_surface(_MAIN)
    lost = sorted(r1 - wide)
    assert not lost, (
        "the widened predicate LOST lanes the round-1 predicate saw — a narrowing hid "
        f"behind a widening: {lost}")


def test_classify_fact_3d_keeps_its_barrier():
    """The round-2 fix pin: the l4_alias_probe read transaction is released before the
    helper returns, so the caller's per-edge thread hops can never idle on it. Revert-red:
    removing the release_read_transaction call turns this red by name."""
    funcs = _top_level_functions(_module(_MAIN))
    lane = funcs.get("classify_fact_3d")
    assert lane is not None, "main.py::classify_fact_3d renamed/removed — re-anchor this pin"
    assert _BARRIER in _referenced_names(lane), (
        "main.py::classify_fact_3d lost its read barrier — this is the fix for the 30.2s "
        "l4_alias_probe hold measured in the round-1 window; re-land it")


def test_ingest_probe_sites_keep_their_barriers():
    """The two ingest-side read sites measured holding 29.8s (round-1 window) keep their
    barriers: the sp_retype_subj_type probe and the subject-type self-heal fallback."""
    refs = _referenced_names(_top_level_functions(_module(_MAIN))["ingest"])
    assert "release_read_transaction" in refs, (
        "main.py::ingest lost all read barriers — the retype-probe and self-heal barriers "
        "landed in round 2; re-land them")
    src = _MAIN.read_text(encoding="utf-8")
    for ctx in ("ingest_retype_subject_type_probe", "ingest_subject_type_selfheal"):
        assert ctx in src, (
            f"ingest barrier context {ctx} vanished — re-measure before assuming the "
            "29.8s read-hold class changed with it")
