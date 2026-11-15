"""Suite-wide test environment — the SINGLE place that touches process-wide state.

WHY THIS FILE EXISTS
────────────────────
~50 test modules used to assign ``SAAS_MODE`` / ``SAAS_OPERATOR_KEY`` / ``SAAS_ADMIN_HOSTS`` /
``SAAS_TENANT_HOSTS`` / ``POSTGRES_DSN`` straight into ``os.environ`` at MODULE IMPORT time.
``os.environ`` is process-global and pytest imports every test module up front, so whichever
module happened to be imported first decided those values for the WHOLE session — and
``src/mcp/http_server.py`` snapshots the host lists into frozensets exactly once, at ITS import.
Net effect: a module's pass/fail depended on collection ORDER. Symptoms were 404-where-403 was
expected, blanket 503s, and ``psycopg2.OperationalError: could not translate host name "unused"``
in the DB-backed modules (one module hard-set a placeholder DSN, defeating another's
"skip when there is no database" guard).

THE CONTRACT NOW
────────────────
1. A test module must NOT write to ``os.environ`` at import time. Nothing here reaches into a
   test module; test modules reach in here.
2. This conftest establishes ONE deterministic baseline before any test module is imported
   (conftest.py is imported first, by pytest, by design). It only ``setdefault``s — WITH THE ONE
   DOCUMENTED R12 EXCEPTION: ``SPACY_MODEL`` is FORCED to the house model (see below; a
   non-loadable ambient model poisons the parser pool process-wide via _get_nlp's permanent
   failure cache), so for that one key the caller's environment does NOT win.
3. Anything a module needs BEYOND the baseline it takes per-test via ``monkeypatch`` or one of
   the fixtures below — function-scoped, and therefore restored afterwards.

``SAAS_MODE`` is part of the baseline rather than a fixture because ``src/mcp/http_server.py``
mounts the entire SaaS router set at IMPORT time; a fixture runs far too late to affect that.
Tests that need the open-core (SaaS-off) behaviour already flip it per test with
``monkeypatch.delenv``/``setenv``, or patch the resolver directly — both still work.
"""

import os

# ── the baseline, applied at conftest import (i.e. before ANY test module is imported) ──
# setdefault, never assignment: a real environment value provided by the runner still wins.
os.environ.setdefault("SAAS_MODE", "1")
os.environ.setdefault("SAAS_SESSION_SECRET", "faultline-test-signing-secret-0123456789")
os.environ.setdefault("MCP_API_KEY", "unit-test-key")
os.environ.setdefault("SAAS_COOKIE_SECURE", "false")  # cookie_secure() needs no request scheme
os.environ.setdefault("SAAS_SIGNUP_LEDGER_SALT", "faultline-test-ledger-salt-xyz")
# The spaCy model is ENVIRONMENT, not behaviour: production and the faultline-wgm image both set
# it (CLAUDE.md "SPACY_MODEL=en_core_web_sm ... set on prod"), and without it the spine deriver
# silently no-ops. ~60 modules used to setdefault it at import, which leaked it suite-wide; now
# that import-time writes are module-scoped (below) the suite states it once, here.
# GAUNTLET R12 (ticket-06): this is the ONE deliberate exception to the setdefault contract
# above — _get_nlp (linguistics.py) caches a FAILED load permanently (_load_attempted, never
# retried), so an ambient SPACY_MODEL=bogus poisons the parser pool process-wide before any
# file-local fixture can run (critic-measured: 20-21 parse-dependent pins red in a hostile
# full-suite run). A runner-supplied NON-LOADABLE model is not a meaningful test configuration
# for this suite; the baseline forces the house model instead of merely defaulting it.
os.environ["SPACY_MODEL"] = "en_core_web_sm"
# Likewise the brain's model id: configuration every deployment sets (docker-compose, prod), which
# LLM-lane builders require before they will call the (always stubbed, in tests) LLM. Six modules
# setdefault'ed it at import; pins such as test_db_read_no_idle_in_transaction's extraction
# builders and test_seed_reltype_template_preservation only passed on that leak.
os.environ.setdefault("WGM_LLM_MODEL", "qwen/qwen3.5-9b")

# GAUNTLET R12: the rel-vocab veto levers default ON and are read live at the growth seams;
# hostile ambient values spuriously redden the vocab pins (critic-measured). Per-test
# setenv still works (function-scoped monkeypatch overrides the baseline).
os.environ.pop("REL_TYPE_NEGATION_VETO", None)
os.environ.pop("REL_TYPE_CITATION_VETO", None)
os.environ.pop("REL_TYPE_VOCAB_NORMALIZER", None)

# The DSN the RUNNER gave us, captured before any test module can touch it. A handful of
# extraction-layer modules ``os.environ.pop("POSTGRES_DSN")`` at import so their overlays fall
# back to code defaults — process-wide and permanent, so by the time the DB-backed suites RUN the
# real DSN is simply gone and they would skip a database that is sitting right there. Tests take
# it back through the ``requires_postgres`` fixture, per test.
_RUNNER_POSTGRES_DSN = os.environ.get("POSTGRES_DSN") or None

# The baseline environment itself, frozen. This is the state under which src/mcp/http_server.py
# gets imported and therefore the ONLY state a reload of it may legitimately be left in — see
# ``reload_http_server``.
_BASELINE_ENV = dict(os.environ)

# Deliberately NOT part of the baseline — these are the values whose per-module conflicts caused
# the order-dependence, so the suite leaves them UNSET and each module takes what it needs from a
# fixture: SAAS_OPERATOR_KEY, SAAS_ADMIN_HOSTS, SAAS_TENANT_HOSTS, SAAS_VOLENTI_HOSTS,
# SAAS_TENANT_BASE_DOMAINS, POSTGRES_DSN.

import importlib  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# import-time os.environ writes by TEST MODULES are scoped to that module (#63)
# ─────────────────────────────────────────────────────────────────────────────
#
# Contract rule 1 above says a test module must not write os.environ at import. ~80 modules do
# anyway (`os.environ.setdefault("SENTENCE_PIPELINE", "true")`, `os.environ.pop("POSTGRES_DSN")`,
# ...). pytest imports EVERY module during collection, before ANY test runs, so each of those
# writes applied to the whole session: a flag one module wanted flipped every call-time read in
# every other module (measured on the open-source tree: ~50 extra failures in a whole-suite run,
# each file green on its own), and a DSN pop made later modules compute `_DSN = ""` and skip their
# DB half. The harness now enforces the contract at the cause instead of trusting 80 files:
#
#   1. While a test MODULE is being collected (imported), writes to os.environ whose nearest
#      non-stdlib caller is a file under tests/ (not a conftest) are recorded against that file.
#   2. When that module's collection ends, those writes are undone, so the next module imports
#      under the suite baseline, exactly as it would in isolation.
#   3. A module-scoped autouse fixture replays the module's own writes (env + the same-named
#      constant on already-imported src./saas. modules, the way foss-increment-1's smoke fixture
#      does it) for that module's tests only, and undoes them afterwards.
#
# Writes made by src/ code during an import are NOT touched (a library may legitimately set its
# own env at import). What this cannot undo generically: a src module that was FIRST imported
# inside some test module's window baked that module's env into a differently-named constant.
# That was already true (and worse) before; the replay covers the same-named case.

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
import _collections_abc  # noqa: E402

# The frames between a caller and os._Environ.__setitem__ (setdefault/pop/update live in these).
# Compared by exact path, plus the "<frozen ...>" names 3.11+ gives frozen stdlib modules.
_STDLIB_ENV_FILES = frozenset({os.__file__, _collections_abc.__file__,
                               "<frozen os>", "<frozen _collections_abc>"})
_MODULE_ENV_WRITES: dict = {}   # realpath(test file) -> [(key, value-or-None-for-delete), ...]
_COLLECT_WINDOW: list = []      # stack of per-window undo logs: [(key, previous-or-None), ...]


def _env_writer_test_file():
    """The test file whose code performed the current os.environ write, else None."""
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if fn in _STDLIB_ENV_FILES:
            f = f.f_back
            continue
        fn = os.path.realpath(fn)
        if fn.startswith(_TESTS_DIR + os.sep) and os.path.basename(fn) != "conftest.py":
            return fn
        return None
    return None


_orig_env_setitem = os._Environ.__setitem__
_orig_env_delitem = os._Environ.__delitem__


def _recording_setitem(self, key, value):
    if self is os.environ and _COLLECT_WINDOW:
        owner = _env_writer_test_file()
        if owner is not None:
            _COLLECT_WINDOW[-1].append((key, os.environ.get(key)))
            _MODULE_ENV_WRITES.setdefault(owner, []).append((key, value))
    return _orig_env_setitem(self, key, value)


def _recording_delitem(self, key):
    if self is os.environ and _COLLECT_WINDOW:
        owner = _env_writer_test_file()
        if owner is not None:
            _COLLECT_WINDOW[-1].append((key, os.environ.get(key)))
            _MODULE_ENV_WRITES.setdefault(owner, []).append((key, None))
    return _orig_env_delitem(self, key)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    """Bracket each test MODULE's import so its import-time env writes cannot leak (#63)."""
    if not isinstance(collector, pytest.Module):
        return (yield)
    _COLLECT_WINDOW.append([])
    os._Environ.__setitem__ = _recording_setitem
    os._Environ.__delitem__ = _recording_delitem
    try:
        return (yield)
    finally:
        undo = _COLLECT_WINDOW.pop()
        if not _COLLECT_WINDOW:
            os._Environ.__setitem__ = _orig_env_setitem
            os._Environ.__delitem__ = _orig_env_delitem
        for key, prev in reversed(undo):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


def _coerce_like(current, raw: str):
    """Parse an env string into the type of an already-bound module constant, or raise."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, str):
        return raw
    raise TypeError(type(current).__name__)


def _apply_env(mp, writes):
    """Apply [(key, value-or-None)] to os.environ AND to the same-named, already-bound constant of
    every loaded src./saas. module, through MonkeyPatch so it is undone with it."""
    for key, value in writes:
        if value is None:
            mp.delenv(key, raising=False)
            continue
        mp.setenv(key, value)
        for modname, mod in list(sys.modules.items()):
            if mod is None or not modname.startswith(("src.", "saas.")):
                continue
            cur = getattr(mod, key, None) if key in getattr(mod, "__dict__", {}) else None
            if cur is None:
                continue
            try:
                mp.setattr(mod, key, _coerce_like(cur, value))
            except (TypeError, ValueError):
                continue


@pytest.fixture(scope="module", autouse=True)
def _fresh_metadata_caches():
    """Start every module with empty process-global metadata caches (#44).

    src.ontology.canonical and the rel_type / taxonomy / linguistic-cue overlays cache per SCHEMA,
    not per DSN. A test that points POSTGRES_DSN at its own throwaway database (a fresh boot db,
    an unseeded mint db) leaves maps loaded from THAT database cached under the default key, and
    the next module reads them against the suite database: measured as test_linguistics' minted-
    rel convergence pins going red whole-suite and green alone, and test_ingest_spine's
    is_relation_fit('has'). In production the DSN is constant, so this is harness state, not a
    product cache bug. Only modules already imported are touched (no import cost)."""
    for modname, fn, args in (("src.ontology.canonical", "reset_caches", ()),
                              ("src.api.rel_type_overlay", "invalidate", (None,)),
                              ("src.api.taxonomy_overlay", "invalidate", (None,)),
                              ("src.api.linguistic_cue_overlay", "invalidate", (None,))):
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, fn):
            try:
                getattr(mod, fn)(*args)
            except Exception:
                pass
    yield


@pytest.fixture(autouse=True)
def _no_leaked_tenant_schema_binding():
    """A test that binds the overlay's request-scoped tenant schema must not leave it bound (#44).

    rel_type_overlay._current_schema is a ContextVar, and pytest runs every test in one context,
    so a `set_current_schema(...)` without a reset (several tests do exactly that) makes every
    later schema-blind overlay read on that worker resolve against THAT test's throwaway tenant
    (often already dropped or truncated). Reset to the default after each test."""
    yield
    rto = sys.modules.get("src.api.rel_type_overlay")
    var = getattr(rto, "_current_schema", None) if rto is not None else None
    if var is not None:
        try:
            if var.get() is not None:
                var.set(None)
        except Exception:
            pass


@pytest.fixture(scope="module", autouse=True)
def _module_import_env(request):
    """Replay this module's own import-time env writes for its tests only, then undo (#63)."""
    writes = _MODULE_ENV_WRITES.get(os.path.realpath(str(request.path)))
    if not writes:
        yield
        return
    mp = pytest.MonkeyPatch()
    _apply_env(mp, writes)
    yield
    mp.undo()


# The production values of the capture/query flags, read from the running production container
# with an anchored per-name `printenv | grep -E "^(NAME|...)="` on 2026-09-29 (never an unfiltered
# dump). A module whose pins are about production behaviour opts in with
#     pytestmark = pytest.mark.usefixtures("production_flags")
# instead of os.environ.setdefault at import (#63). Re-measure before trusting after a deploy.
PRODUCTION_FLAGS = {
    "SENTENCE_PIPELINE": "true",
    "SPINE_NAMING_CHAIN": "true",
    "SPINE_PENDING_GROUNDING": "true",
    "SPINE_COVERAGE_GATE": "true",
    "SPINE_PA_CORE": "1",
    "SPINE_POSSESSIVE_ALIENABILITY": "true",
    "SPINE_IMPLICATIVE_XCOMP": "true",
    "L4_WORDNET_LADDER": "true",
    "VALUE_PLACE_FIRST_CLASS": "1",
    "SCOPE_SURFACE_CONTEXT_C": "0",
    "QUERY_NAME_INTENT_SURFACING": "1",
    "QUERY_WALK_READS_ATTRIBUTES": "1",
    "SPACY_MODEL": "en_core_web_sm",
}


@pytest.fixture(scope="module")
def production_flags():
    """PRODUCTION_FLAGS for one module (env + same-named constants), undone afterwards."""
    import src.api.main  # noqa: F401 — bind the constants before patching them
    import src.extraction.linguistics  # noqa: F401
    mp = pytest.MonkeyPatch()
    _apply_env(mp, list(PRODUCTION_FLAGS.items()))
    yield dict(PRODUCTION_FLAGS)
    mp.undo()


@pytest.fixture(scope="module")
def no_tenant_db():
    """Run a module of pure deriver pins with NO database, i.e. on the in-code cue floors.

    These modules never touch a database themselves, but the deriver silently reads the
    per-tenant / public cue overlays whenever POSTGRES_DSN is set, and its output then differs
    (#75). They were written and verified without a DSN, and every
    whole-suite run executed them without one (an import-time os.environ.pop('POSTGRES_DSN')
    elsewhere, #63). This declares that environment instead of inheriting it by accident."""
    mp = pytest.MonkeyPatch()
    mp.delenv("POSTGRES_DSN", raising=False)
    yield
    mp.undo()


def pytest_configure(config):
    """Register `xdist_group` (#44) so a run without xdist does not warn about it.

    Suites that mutate ONE shared control-plane row chain, or apply a fan-out migration to the
    shared suite database, carry `@pytest.mark.xdist_group(<name>)`. xdist honours the mark only
    under `--dist loadgroup`; run the floor with it to serialise those suites:

        pytest tests -n 8 --dist loadgroup

    It is deliberately NOT switched on automatically. Measured on the truenas floor: xdist 3.8's
    LoadScope/LoadGroup scheduler aborts the whole session (`INTERNALERROR KeyError:
    <WorkerController gwN>`) when it replaces a crashed worker, and workers there get OOM-killed
    under memory pressure; plain `load` survives the same crash. The suites in those groups own
    their state (see each module), so they are correct under `load` too; the group only removes
    the remaining cross-worker interleaving.
    """
    config.addinivalue_line(
        "markers", "xdist_group(name): run every test carrying this group on one xdist worker")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Under an explicit `--dist loadgroup`, a module with no group of its own is its OWN group, so
    a module's tests never split across workers (fixtures here name throwaway resources per
    module). Runs before xdist's own hook reads the marks (tryfirst)."""
    worker_lg = getattr(config, "workerinput", None) is not None and config.getvalue("loadgroup")
    master_lg = getattr(config.option, "dist", None) == "loadgroup"
    if not (worker_lg or master_lg):
        return
    for item in items:
        if item.get_closest_marker("xdist_group") is None:
            item.add_marker(pytest.mark.xdist_group(name=item.nodeid.split("::", 1)[0]))


_IDLE_TXN_LEAKS: list = []


def idle_in_transaction_backends(app_prefix: str, min_idle_s: float = 0.0) -> list:
    """[(pid, application_name, age, query)] idle in transaction under an application_name prefix.

    ``min_idle_s`` drops a backend that went idle only a moment ago: xdist can split one module
    across workers, so at one worker's module end a sibling worker may be mid-test in the same
    module with a transaction legitimately open for a second."""
    if not _RUNNER_POSTGRES_DSN:
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(_RUNNER_POSTGRES_DSN, connect_timeout=3,
                                application_name="fl:idle-txn-probe")
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pid, application_name, (now() - state_change)::text, left(query, 160)"
                    "  FROM pg_stat_activity WHERE state LIKE 'idle in transaction%%'"
                    "   AND left(application_name, %s) = %s"
                    "   AND now() - state_change >= %s * interval '1 second'",
                    (len(app_prefix), app_prefix, min_idle_s))
                return cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return []


@pytest.fixture(scope="module", autouse=True)
def _module_leaves_no_idle_transaction(request):
    """Report any backend a module's tests left IDLE IN TRANSACTION (#44, the suite hang).

    With PGAPPNAME tagging (below) every backend names the test that opened it. At module end,
    one query finds backends still idle in transaction under this module's tests — the exact
    precondition of the 2026-09-29 hang (a migration's ACCESS EXCLUSIVE lock queued behind one
    for 49 minutes). Reported in the terminal summary and appended to
    $FL_IDLE_TXN_REPORT when set; it does not fail the module (the leak is the other test's).
    """
    yield
    rows = idle_in_transaction_backends(("fl:" + request.node.nodeid)[:63], min_idle_s=10.0)
    for row in rows:
        line = f"IDLE-IN-TRANSACTION left by {row[1]} (pid {row[0]}, {row[2]}): {row[3]}"
        _IDLE_TXN_LEAKS.append(line)
        report = os.environ.get("FL_IDLE_TXN_REPORT")
        if report:
            try:
                with open(report, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


def pytest_terminal_summary(terminalreporter):
    if _IDLE_TXN_LEAKS:
        terminalreporter.section("connections left idle in transaction (#44)")
        for line in _IDLE_TXN_LEAKS:
            terminalreporter.write_line(line)


@pytest.fixture(autouse=True)
def _test_env_is_restored():
    """A TEST's own os.environ writes end with the test (#63, run-time half).

    ~30 test bodies assign or pop os.environ directly (`os.environ.pop("POSTGRES_DSN")` to force
    an in-memory fallback, `os.environ["POSTGRES_DSN"] = DSN or ""`, ...) and never restore it.
    While import-time pops had already removed the DSN for the whole session nobody noticed;
    with the import-time half fixed, one such test silently removed the DSN for every later
    test on its xdist worker (measured: ~80 `KeyError: 'POSTGRES_DSN'` reds). Snapshot before,
    restore after — monkeypatch-managed changes compose with this in either teardown order.
    """
    before = dict(os.environ)
    yield
    for key in [k for k in os.environ if k not in before and not k.startswith("PYTEST_")]:
        del os.environ[key]
    for key, value in before.items():
        if key.startswith("PYTEST_"):
            continue
        if os.environ.get(key) != value:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _pg_application_name(request, monkeypatch):
    """Tag every libpq connection a test opens with the test's id (PGAPPNAME).

    libpq reads PGAPPNAME at connect time, so pg_stat_activity.application_name names the test
    that opened a backend. That is what turns "some xdist worker left a connection idle in
    transaction for 49 minutes" (#44, the suite hang) into a test id you can go and fix.
    """
    # head-kept (63 = NAMEDATALEN-1): the module path must survive so the module-end probe
    # above can attribute a backend to its module even when the test name is cut.
    monkeypatch.setenv("PGAPPNAME", ("fl:" + request.node.nodeid)[:63])


def _restore_env_for_reload(setup_env: dict) -> None:
    """Undo the test's own env changes, then re-assert the suite baseline on top.

    ``setup_env`` is os.environ as it stood when the fixture was set up, i.e. before the test
    body touched anything. Restoring it removes whatever the test added and puts back whatever
    the test changed — WITHOUT disturbing variables an unrelated module legitimately owns
    (LINGUISTIC_LAYER, SPACY_MODEL, SPINE_*, … are set by extraction modules at their own import,
    long after this conftest ran, so they are absent from the baseline and must survive).
    GAUNTLET R13 CORRECTION: SPACY_MODEL is no longer in that set — since the R12 hermeticity
    fix it IS a baseline key (forced to en_core_web_sm), so the merge below deliberately
    RE-ASSERTS it over any per-test value, keeping a hostile ambient model out of the closing
    reload.

    ``_BASELINE_ENV`` then wins for every key it defines. That is what makes this correct
    underneath an autouse fixture which has ALREADY changed the environment before this fixture
    was set up — tests/mcp/conftest.py deletes SAAS_MODE and MCP_API_KEY, so they are missing
    from ``setup_env``, and reloading without re-asserting them rebuilds http_server with the
    SaaS lane off for the rest of the session.

    PYTEST_* is left alone: pytest owns ``PYTEST_CURRENT_TEST`` and pops it itself during
    teardown, so removing it here turns a clean teardown into ``KeyError: PYTEST_CURRENT_TEST``.
    """
    final = {**setup_env, **_BASELINE_ENV}
    for key in list(os.environ):
        if key not in final and not key.startswith("PYTEST_"):
            del os.environ[key]
    os.environ.update(final)


# ─────────────────────────────────────────────────────────────────────────────
# host-face configuration (the import-time frozensets in src/mcp/http_server.py)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def saas_host_faces(monkeypatch):
    """Configure the http_server host-face lists for ONE test, then restore.

    ``src/mcp/http_server`` parses SAAS_{ADMIN,TENANT,VOLENTI}_HOSTS into frozensets once at
    import, so setting the env after import does nothing — which is exactly why modules used to
    set it at import time and stomp each other. This sets BOTH: the env (so anything that
    re-reads it, e.g. ``_derive_tenant_base_domains``, agrees) and the already-bound module
    attributes, via monkeypatch so both are undone at teardown.

    The values still go through the module's OWN ``_parse_host_list`` / ``_derive_tenant_base_domains``,
    so the parsing/derivation logic remains under test — nothing is hand-rolled here.

        def test_x(saas_host_faces):
            h = saas_host_faces(admin="fcontrol.example.test", tenant="app.example.test")
    """
    def _apply(admin="", tenant="", volenti="", base_domains=None):
        import src.mcp.http_server as h

        for name, raw in (("SAAS_ADMIN_HOSTS", admin),
                          ("SAAS_TENANT_HOSTS", tenant),
                          ("SAAS_VOLENTI_HOSTS", volenti)):
            if raw:
                monkeypatch.setenv(name, raw)
            else:
                monkeypatch.delenv(name, raising=False)
            monkeypatch.setattr(h, name, h._parse_host_list(raw))

        if base_domains is None:
            monkeypatch.delenv("SAAS_TENANT_BASE_DOMAINS", raising=False)
        else:
            monkeypatch.setenv("SAAS_TENANT_BASE_DOMAINS", base_domains)
        monkeypatch.setattr(h, "SAAS_TENANT_BASE_DOMAINS", h._derive_tenant_base_domains())
        return h

    return _apply


@pytest.fixture
def reload_http_server():
    """Re-import ``src.mcp.http_server`` inside a test WITHOUT leaking the reload.

    ``importlib.reload`` on this module is process-wide and destructive: it rebuilds ``h.app``
    and re-reads every import-time env snapshot, so a reload left behind by one test silently
    re-decided the routing surface for every module that ran after it. (Two modules did exactly
    this; one of them even reloaded from inside a ``finally`` AFTER deleting SAAS_MODE, leaving
    the whole session with an app that had no SaaS routers mounted at all.)

    The closing reload happens under the test's pre-test environment with ``_BASELINE_ENV``
    re-asserted on top (see ``_restore_env_for_reload``), so http_server is left in the state it
    was FIRST imported in — the only state the rest of the suite is entitled to assume — while
    variables owned by unrelated modules are left untouched. Those values are what monkeypatch
    will restore too, so the two are idempotent in either teardown order.
    """
    import src.mcp.http_server as h

    setup_env = dict(os.environ)
    yield lambda: importlib.reload(h)
    _restore_env_for_reload(setup_env)
    importlib.reload(h)


# ─────────────────────────────────────────────────────────────────────────────
# control-plane database availability
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def postgres_dsn():
    """The runner's control-plane DSN if a database is actually REACHABLE, else None.

    A truthy POSTGRES_DSN is not evidence of a database: a module used to plant a placeholder
    (``postgresql://unused/...``) into os.environ at import, process-wide, which turned a clean
    "no DB → skip" into ``OperationalError: could not translate host name "unused"``. So this
    connects once, and the answer is the connection — not the string.
    """
    if not _RUNNER_POSTGRES_DSN:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(_RUNNER_POSTGRES_DSN, connect_timeout=5)
        conn.close()
        return _RUNNER_POSTGRES_DSN
    except Exception:
        return None


@pytest.fixture
def requires_postgres(postgres_dsn, monkeypatch):
    """Skip (honestly) when there is no reachable control-plane database; otherwise guarantee
    POSTGRES_DSN is in the environment for the duration of the test.

    Use at module scope:  ``pytestmark = pytest.mark.usefixtures("requires_postgres")``
    """
    if postgres_dsn is None:
        pytest.skip("no reachable control-plane database (POSTGRES_DSN unset or unreachable)")
    monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
    return postgres_dsn


@pytest.fixture
def apply_migration_files():
    """The FOSS migration convention (docker-entrypoint runs every ``migrations/*.sql``
    via psql at boot, idempotently) expressed for tests: apply the whole corpus, sorted,
    each file as one batch, autocommit. (FOSS-es conftest fixture, preserved through the
    saas-lineage conftest sync.)"""
    import glob as _glob
    import psycopg2

    def _apply(dsn):
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            for path in sorted(_glob.glob(os.path.join(os.path.dirname(__file__), "..", "migrations", "*.sql"))):
                with open(path, "r", encoding="utf-8") as fh:
                    cur.execute(fh.read())
        conn.close()

    return _apply
