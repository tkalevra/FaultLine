"""Gauntlet ``lme-reset-reseed`` — round 1 pins.

benchmarks/harness/run_longmemeval.py::reset_tenant TRUNCATEd the bench seat (its own identity anchor
in ``entities`` / ``entity_aliases`` included, plus the seeded ``retraction_signals``) and put back only
``rel_types`` — so every LongMemEval run to date scored on a STRIPPED seat: no user anchor to walk from,
no seeded structure. Measured on a fresh throwaway tenant at 5aa1992b (round_1/repro.txt): after the
reset a seat differed from a freshly provisioned tenant on entities (1→0), entity_aliases (2→0),
retraction_signals (10→0).

The fix runs the PROVISIONER'S OWN ``schema_manager.seed_tenant_metadata`` after the truncate, through
the only door the off-box harness has — a driver on the stdin of ``docker exec -i <api> python3 -`` —
never a psql re-implementation of the seed.

  LR1 — the wound: the reset WITHOUT its reseed strips the seat (full per-table set-diff vs fresh); and,
        sha-pinned, the pristine 5aa1992b file does the same through the real transport.
  LR2 — the reseed goes through the exec transport with the driver text on stdin; the driver imports and
        calls ``seed_tenant_metadata``; an AST census proves the harness carries no ``INSERT INTO`` for
        any table the provisioner seeds (no second seed list).
  LR3 — parity: a seat that was grown, corrupted in place and littered is row-set IDENTICAL to a fresh
        tenant on EVERY base table after the reset; a failing reseed raises out of ``reset_tenant`` AND
        out of ``_deterministic_pass`` before any ingest (never a stripped-seat score).
  LR4 — revert-red: ``LME_RESEED_HARNESS_PATH=<pristine copy>`` makes LR2/LR3 load the old file and go red
        (round_1/revert_red.txt).

Driven, not stubbed: the TRUNCATE runs over the harness's own ``docker exec … psql`` (LOCAL mode) against
a throwaway database, the reseed over a real ``docker exec -i … python3 -`` into a container started from
the api image with this tree mounted (the running api container's image predates the shared seed), and
the Qdrant delete against a throwaway collection on the local Qdrant. Only ``flush_redis`` (FLUSHALL on the
shared local redis) is replaced, by a recorder. Needs POSTGRES_DSN → a database whose name contains
"test" (never ``faultline``), docker, and the local stack; skips honestly otherwise.
"""
from __future__ import annotations

import ast
import base64
import importlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "benchmarks" / "harness" / "run_longmemeval.py"
PRISTINE_SHA = "5aa1992b"  # the measured wound; `git show` of this sha survives HEAD moving

pytestmark = pytest.mark.usefixtures("requires_postgres")

_real_connect = psycopg2.connect


# ─────────────────────────────────────────────────────────────────────────────
# environment
# ─────────────────────────────────────────────────────────────────────────────

def _dsn_or_skip(dsn):
    if not dsn:
        pytest.skip("POSTGRES_DSN not set/unreachable — these pins need a live throwaway Postgres")
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in dbname or dbname == "faultline":
        pytest.skip(f"refusing to run against database {dbname!r} (name must contain 'test', never 'faultline')")
    try:
        _real_connect(dsn, connect_timeout=3).close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    return dsn


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _docker_or_skip():
    try:
        r = _docker("version", "--format", "{{.Server.Version}}")
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"docker unavailable: {e}")
    if r.returncode != 0:
        pytest.skip(f"docker daemon unreachable: {r.stderr.strip()[:120]}")


@pytest.fixture(scope="module")
def dsn(postgres_dsn):
    return _dsn_or_skip(postgres_dsn)


@pytest.fixture(scope="module")
def rig(dsn):
    """The local stack the harness's LOCAL mode drives: the postgres container the throwaway db lives
    in (the TRUNCATE goes through ``docker exec <pg> psql``), its network, and the api image."""
    _docker_or_skip()
    pg = os.environ.get("BENCH_PG_CONTAINER", "faultline-postgres")
    r = _docker("inspect", pg, "--format",
                "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{$v.IPAddress}}{{end}}")
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip(f"postgres container {pg!r} not running locally")
    net, ip = r.stdout.split()[:2]
    api = os.environ.get("BENCH_API_IMAGE_SOURCE", "faultline-api")
    r = _docker("inspect", api, "--format", "{{.Config.Image}}")
    image = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "faultline:latest"
    # the db the DSN names must be the one the postgres container serves
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0]
    r = _docker("exec", pg, "psql", "-U", os.environ.get("BENCH_PG_USER", "faultline"), "-d", dbname,
                "-tAc", "SELECT 1")
    if r.returncode != 0 or r.stdout.strip() != "1":
        pytest.skip(f"database {dbname!r} not reachable through `docker exec {pg} psql`: {r.stderr.strip()[:120]}")
    return {"pg": pg, "net": net, "pg_ip": ip, "image": image, "db": dbname}


@pytest.fixture(scope="module")
def exec_container(rig, dsn):
    """A throwaway container from the api image with THIS tree mounted at /app: the target of the
    harness's ``docker exec -i <api> python3 -``. The running api container's image was built before
    ``seed_tenant_metadata`` existed, so it cannot host the driver; a sibling container from the same
    image on the same network, carrying the current tree, is the same door with current code."""
    name = f"lme-reseed-exec-{os.getpid()}"
    # the container reaches postgres over the docker network, not the runner's loopback
    in_net_dsn = re.sub(r"@(localhost|127\.0\.0\.1)(:|/)", f"@{rig['pg_ip']}\\2", dsn)
    _docker("rm", "-f", name)
    r = _docker("run", "-d", "--name", name, "--network", rig["net"], "-v", f"{REPO}:/app:ro", "-w", "/app",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", f"POSTGRES_DSN={in_net_dsn}",
                "--entrypoint", "sleep", rig["image"], "3600", timeout=120)
    if r.returncode != 0:
        pytest.skip(f"could not start the exec container from {rig['image']}: {r.stderr.strip()[:160]}")
    try:
        yield name
    finally:
        _docker("rm", "-f", name)


@pytest.fixture(scope="module")
def qdrant_collection():
    """A throwaway collection on the local Qdrant so the reset's filtered Class-C delete runs for real
    (the harness raises on anything but HTTP 200 — a 404 on a missing collection is the wound 4e0c5518)."""
    url = os.environ.get("BENCH_QDRANT_URL", "http://localhost:6333").rstrip("/")
    name = f"lme-reseed-test-{os.getpid()}"
    body = json.dumps({"vectors": {"size": 4, "distance": "Cosine"}}).encode()
    req = urllib.request.Request(f"{url}/collections/{name}", data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected — local test rig URL from env
    except Exception as e:
        pytest.skip(f"local Qdrant at {url} unavailable: {type(e).__name__}: {str(e)[:100]}")
    try:
        yield url, name
    finally:
        try:
            urllib.request.urlopen(urllib.request.Request(f"{url}/collections/{name}", method="DELETE"),
                                   timeout=10).read()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected — local test rig URL from env
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# tenants
# ─────────────────────────────────────────────────────────────────────────────

def _provision(dsn, monkeypatch):
    """A REAL provisioned tenant (create_user_schema) on the throwaway db."""
    from src.provisioning.schema_manager import (create_user_schema, derive_user_slug_from_uuid,
                                                 derive_schema_name)
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    monkeypatch.setenv("FAULTLINE_MIGRATIONS_DIR", str(REPO / "migrations"))
    monkeypatch.setenv("PROVISIONING_CORTEX_BUDGET_SEC", "2")  # the cortex (flagent_*) is not under test
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:1")
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    db = _real_connect(dsn)
    try:
        schema, status = create_user_schema(user_id, slug, db)
    finally:
        db.close()
    assert status == "ready", status
    assert schema == derive_schema_name(slug)
    c = _real_connect(dsn)
    with c.cursor() as cur:
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING", (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                    "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING", (user_id, schema))
    c.commit()
    c.close()
    return {"user_id": user_id, "schema": schema, "dsn": dsn}


def _drop(t):
    c = _real_connect(t["dsn"])
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{t["schema"]}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{t["user_id"].replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (t["user_id"],))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (t["user_id"],))
    finally:
        c.close()


def _snapshot(dsn: str, schema: str, user_id: str) -> dict[str, set]:
    """Every base table's row-set, normalised (the kinship lane's kn1 shape): timestamp/date-typed
    columns dropped (they record WHEN a row was written), serial ids dropped (RESTART IDENTITY), and
    the seat's own uuid / slug replaced by a placeholder (fresh and reset are different seats)."""
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    slug = derive_user_slug_from_uuid(user_id)
    out: dict[str, set] = {}
    c = _real_connect(dsn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY 1", (schema,))
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                cur.execute("SELECT column_name, data_type FROM information_schema.columns"
                            " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (schema, t))
                cols = [cn for cn, dt in cur.fetchall()
                        if not dt.startswith("timestamp") and dt != "date" and cn != "id"]
                if not cols:
                    cur.execute(f'SELECT count(*) FROM "{schema}"."{t}"')
                    out[t] = {("COUNT", cur.fetchone()[0])}
                    continue
                cur.execute(f'SELECT {", ".join(chr(34) + x + chr(34) for x in cols)} FROM "{schema}"."{t}"')
                rows = set()
                for r in cur.fetchall():
                    rows.add(tuple(str(v).replace(user_id, "<seat>").replace(slug, "<slug>") for v in r))
                out[t] = rows
    finally:
        c.close()
    return out


def _diff(a: dict[str, set], b: dict[str, set]) -> dict[str, tuple[int, int]]:
    """table → (rows fresh has that reset lacks, rows reset has that fresh lacks)."""
    assert set(a) == set(b), sorted(set(a) ^ set(b))
    return {t: (len(a[t] - b[t]), len(b[t] - a[t])) for t in sorted(a) if a[t] != b[t]}


def _fmt_diff(a, b, diffs, limit=3):
    lines = []
    for t, (miss, extra) in diffs.items():
        lines.append(f"  {t:32s} missing_after_reset={miss} extra_after_reset={extra} "
                     f"fresh={len(a[t])} reset={len(b[t])}")
        for r in sorted(a[t] - b[t])[:limit]:
            lines.append(f"      fresh-only: {r}")
        for r in sorted(b[t] - a[t])[:limit]:
            lines.append(f"      reset-only: {r}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# the harness under test
# ─────────────────────────────────────────────────────────────────────────────

def _harness_path() -> Path:
    """The fixed harness, or the file LME_RESEED_HARNESS_PATH names (the revert-red lever: point it at a
    pristine copy and LR2/LR3 must go red)."""
    override = os.environ.get("LME_RESEED_HARNESS_PATH", "").strip()
    return Path(override) if override else HARNESS


def _pristine_copy(tmp_path_factory) -> Path:
    r = subprocess.run(["git", "-C", str(REPO), "show", f"{PRISTINE_SHA}:benchmarks/harness/run_longmemeval.py"],
                       capture_output=True, text=True)
    if r.returncode != 0 or "def reset_tenant" not in r.stdout:
        pytest.skip(f"git show {PRISTINE_SHA}:… unavailable in this checkout: {r.stderr.strip()[:100]}")
    p = tmp_path_factory.mktemp("pristine") / "run_longmemeval_5aa1992b.py"
    p.write_text(r.stdout)
    return p


def _load_harness(path: Path, seat: str, rig, exec_container, qdrant_collection, monkeypatch, *,
                  name: str):
    """Import a run_longmemeval file under a unique module name with the bench env bound to the local
    rig, LOCAL mode, the throwaway db/collection, and THIS seat declared disposable (two-key)."""
    qurl, qcoll = qdrant_collection
    for k, v in {"BENCH_LOCAL": "1", "BENCH_USER_ID": seat, "BENCH_DISPOSABLE_SEAT": seat,
                 "BENCH_PG_CONTAINER": rig["pg"], "BENCH_PG_DB": rig["db"],
                 "BENCH_API_CONTAINER": exec_container, "BENCH_QDRANT_URL": qurl,
                 "BENCH_QDRANT_COLLECTION": qcoll, "FAULTLINE_TOKEN": "unused-in-direct-mcp",
                 "BENCH_EXECUTOR": "direct_mcp", "BENCH_RESET_MAX_TRIES": "2"}.items():
        monkeypatch.setenv(k, v)
    if str(HARNESS.parent) not in sys.path:  # a pristine copy in tmp still imports owui_pathway & co.
        sys.path.insert(0, str(HARNESS.parent))
    spec = importlib.util.spec_from_file_location(name, str(path))
    rl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rl)
    assert rl.LOCAL_MODE and rl.PG_DB == rig["db"] and rl.BENCH_UUID == seat
    # FLUSHALL on the shared local redis is the one step not driven — recorded instead
    rl._flushes = []
    rl.flush_redis = lambda: rl._flushes.append(True)
    return rl


def _spy_run_cmd(rl):
    """Record every rig command the harness issues (cmd, stdin) while still RUNNING it."""
    real = rl._run_cmd
    calls: list[tuple] = []

    def spy(cmd, timeout=120, **kw):
        calls.append((list(cmd) if isinstance(cmd, list) else cmd, kw.get("stdin_text")))
        return real(cmd, timeout=timeout, **kw)
    rl._run_cmd = spy
    return calls


def _seeded_tables_by_ast() -> set[str]:
    """Census the PROVISIONER's seed statements by AST, not a hand list: every ``INSERT INTO <t>``
    string constant reachable from ``schema_manager.seed_tenant_metadata``."""
    sm = importlib.import_module("src.provisioning.schema_manager")
    tree = ast.parse(inspect.getsource(sm))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "seed_tenant_metadata" in fns, "schema_manager.seed_tenant_metadata is missing — no single seed source"
    todo, seen, tables = ["seed_tenant_metadata"], set(), set()
    while todo:
        fname = todo.pop()
        if fname in seen or fname not in fns:
            continue
        seen.add(fname)
        for node in ast.walk(fns[fname]):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                tables.update(t.lower() for t in re.findall(r"INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)",
                                                            node.value, re.I))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                todo.append(node.func.id)
    return tables


def _insert_targets_in_source(src: str) -> set[str]:
    """Every table named by an ``INSERT INTO <t>`` inside ANY string constant of a Python source —
    including the driver text the harness ships on stdin (a string constant like any other)."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.update(t.lower() for t in re.findall(r"INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", node.value, re.I))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# LR1 — the wound
# ═════════════════════════════════════════════════════════════════════════════

def test_lr1_reset_without_its_reseed_strips_the_seat(dsn, rig, exec_container, qdrant_collection, monkeypatch):
    """The reset minus the reseed is what every run to date did. Run the fixed harness's reset with
    ``reseed_tenant`` neutralised — everything else real: the TRUNCATE over ``docker exec psql``, the
    seed-rel delete, the Qdrant delete — and set-diff the seat against a freshly provisioned tenant."""
    fresh = _provision(dsn, monkeypatch)
    reset = _provision(dsn, monkeypatch)
    try:
        rl = _load_harness(_harness_path(), reset["user_id"], rig, exec_container, qdrant_collection,
                           monkeypatch, name="rl_lr1")
        if hasattr(rl, "reseed_tenant"):
            rl.reseed_tenant = lambda: {"ok": True, "neutralised": True}
        calls = _spy_run_cmd(rl)
        rl.reset_tenant()
        assert any(isinstance(c, list) and c[:3] == ["docker", "exec", rig["pg"]] and "psql" in c
                   and "TRUNCATE" in c[-1] for c, _ in calls), "the TRUNCATE did not go through docker exec psql"
        a, b = _snapshot(dsn, fresh["schema"], fresh["user_id"]), _snapshot(dsn, reset["schema"], reset["user_id"])
        diffs = _diff(a, b)
        print("\n[LR1] reset WITHOUT reseed vs fresh tenant:\n" + _fmt_diff(a, b, diffs))
        # the user anchor is gone …
        assert "entities" in diffs and "entity_aliases" in diffs, diffs
        assert not b["entities"], "the seat's own entities row survived — the reset is not the wound"
        assert any(r[0] == "<seat>" and r[1] == "user" for r in a["entity_aliases"] - b["entity_aliases"]), \
            "the preferred 'user' alias should be missing after the un-reseeded reset"
        # … and so is seeded structure the truncate list covers
        assert "retraction_signals" in diffs and not b["retraction_signals"], diffs
    finally:
        _drop(fresh)
        _drop(reset)


def test_lr1b_pristine_file_strips_the_seat_through_the_real_transport(dsn, rig, exec_container, qdrant_collection,
                                                                       monkeypatch, tmp_path_factory):
    """Sha-pinned repro: the file as it was at the measured commit, run through the real docker transport
    against a real provisioned tenant, leaves the seat without its anchor. Survives HEAD moving."""
    pristine = _pristine_copy(tmp_path_factory)
    fresh = _provision(dsn, monkeypatch)
    reset = _provision(dsn, monkeypatch)
    try:
        rl = _load_harness(pristine, reset["user_id"], rig, exec_container, qdrant_collection, monkeypatch,
                           name="rl_pristine_5aa1992b")
        assert not hasattr(rl, "reseed_tenant"), "the pristine copy must predate the reseed"
        rl.reset_tenant()
        a, b = _snapshot(dsn, fresh["schema"], fresh["user_id"]), _snapshot(dsn, reset["schema"], reset["user_id"])
        diffs = _diff(a, b)
        print(f"\n[LR1b] pristine {PRISTINE_SHA} reset vs fresh tenant:\n" + _fmt_diff(a, b, diffs))
        assert {"entities", "entity_aliases", "retraction_signals"} <= set(diffs), diffs
        assert not b["entities"] and not b["entity_aliases"] and not b["retraction_signals"]
    finally:
        _drop(fresh)
        _drop(reset)


# ═════════════════════════════════════════════════════════════════════════════
# LR2 — the transport and the single seed source
# ═════════════════════════════════════════════════════════════════════════════

def test_lr2_reseed_runs_the_provisioners_seed_over_docker_exec_python_stdin(dsn, rig, exec_container,
                                                                             qdrant_collection, monkeypatch):
    """The reset's reseed is ONE ``docker exec -i -e PARAMS_B64=<seat,db> <api> python3 -`` with the driver
    text on stdin; the driver imports ``seed_tenant_metadata`` from ``src.provisioning.schema_manager`` and
    calls it; the harness carries no INSERT of its own for any seeded table (no second seed list)."""
    seeded = _seeded_tables_by_ast()
    for t in ("entities", "entity_aliases", "rel_type_aliases", "entity_taxonomies", "retraction_signals",
              "correction_patterns", "correction_signals"):
        assert t in seeded, (t, sorted(seeded))
    path = _harness_path()
    src = path.read_text()
    overlap = _insert_targets_in_source(src) & seeded
    assert not overlap, f"run_longmemeval carries its own INSERT INTO for seeded tables: {sorted(overlap)}"

    reset = _provision(dsn, monkeypatch)
    try:
        rl = _load_harness(path, reset["user_id"], rig, exec_container, qdrant_collection, monkeypatch,
                           name="rl_lr2")
        assert getattr(rl, "reseed_tenant", None) is not None, "harness has no reseed_tenant — the seat stays stripped"
        driver = getattr(rl, "_RESEED_DRIVER", None)
        assert isinstance(driver, str) and driver.strip(), "no driver text to ship on stdin"
        # the driver is the provisioner's seed, by import — not a re-implementation
        dtree = ast.parse(driver)
        imported = {(n.module, a.name) for n in ast.walk(dtree) if isinstance(n, ast.ImportFrom)
                    for a in n.names}
        assert ("src.provisioning.schema_manager", "seed_tenant_metadata") in imported, imported
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "seed_tenant_metadata"
                   for n in ast.walk(dtree)), "driver never calls seed_tenant_metadata"
        assert not _insert_targets_in_source(driver), "the driver must not INSERT anything itself"

        calls = _spy_run_cmd(rl)
        rl.reset_tenant()
        execs = [(c, s) for c, s in calls if isinstance(c, list) and c[:3] == ["docker", "exec", "-i"]]
        assert len(execs) == 1, f"expected exactly one exec-with-stdin, got {execs!r}"
        cmd, stdin = execs[0]
        assert cmd[-3:] == [exec_container, "python3", "-"], cmd
        assert stdin == driver, "the stdin text is not the driver"
        env = [x for x in cmd if x.startswith("PARAMS_B64=")]
        assert len(env) == 1 and cmd[cmd.index(env[0]) - 1] == "-e", cmd
        params = json.loads(base64.b64decode(env[0][len("PARAMS_B64="):]).decode())
        assert params == {"seat": reset["user_id"], "db": rig["db"]}, params
        # the transport order: truncate → reseed → qdrant delete → (redis flush recorded)
        kinds = ["psql" if (isinstance(c, list) and "psql" in c) else "exec-i" if (isinstance(c, list) and c[:3] == ["docker", "exec", "-i"])
                 else "curl" if (isinstance(c, list) and c and c[0] == "curl") else "other" for c, _ in calls]
        assert kinds.index("exec-i") > kinds.index("psql") and kinds.index("curl") > kinds.index("exec-i"), kinds
        assert rl._flushes == [True]
        # and the seat came back with its anchor
        with _real_connect(dsn) as c, c.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{reset["schema"]}".entities WHERE id = %s', (reset["user_id"],))
            assert cur.fetchone()[0] == 1
            cur.execute(f'SELECT count(*) FROM "{reset["schema"]}".entity_aliases WHERE entity_id = %s AND alias = %s '
                        'AND is_preferred', (reset["user_id"], "user"))
            assert cur.fetchone()[0] == 1
    finally:
        _drop(reset)


# ═════════════════════════════════════════════════════════════════════════════
# LR3 — parity, and a reseed that fails aborts loudly
# ═════════════════════════════════════════════════════════════════════════════

def _litter(dsn, schema, user_id):
    """What a prior question leaves behind: data rows, GROWN metadata, and a seed row CORRUPTED in place
    (the shape the old `DELETE … NOT IN public` restore could never repair)."""
    with _real_connect(dsn) as c, c.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute("INSERT INTO entities (id, entity_type) VALUES ('lr_stray', 'Object')")
        cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred) VALUES ('lr_stray', 'stray thing', true)")
        cur.execute("INSERT INTO facts (subject_id, object_id, rel_type, fact_class) VALUES (%s, 'lr_stray', 'owns', 'A')",
                    (user_id,))
        cur.execute("INSERT INTO rel_types (rel_type, label, source, engine_generated) "
                    "VALUES ('lr_grown_rel', 'lr grown', 'engine', true)")
        cur.execute("INSERT INTO rel_type_aliases (canonical_rel_type, alias, source) VALUES ('owns', 'lr_alias', 'engine')")
        cur.execute("INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types) "
                    "VALUES ('lr_tax', 'grown grouping', ARRAY['Object'])")
        cur.execute("INSERT INTO correction_patterns (pattern_text) VALUES ('lr learned pattern')")
        cur.execute("INSERT INTO climb_state (entity_id, verdict) VALUES ('lr_stray', 'placed')")
        cur.execute("INSERT INTO episodic_log (user_id, raw_text) VALUES (%s, 'I own a stray thing.')", (user_id,))
        # corrupt seed rows IN PLACE: a template clobbered, an alias re-pointed, a seeded signal deleted
        cur.execute("UPDATE rel_types SET natural_language = 'CLOBBERED' WHERE rel_type = 'owns'")
        cur.execute("UPDATE rel_type_aliases SET canonical_rel_type = 'lr_grown_rel' "
                    "WHERE alias = (SELECT min(alias) FROM rel_type_aliases WHERE canonical_rel_type = 'parent_of')")
        cur.execute("DELETE FROM retraction_signals WHERE signal = (SELECT min(signal) FROM retraction_signals)")
        c.commit()


def test_lr3_reset_seat_is_row_set_identical_to_a_fresh_tenant_on_every_table(dsn, rig, exec_container,
                                                                             qdrant_collection, monkeypatch):
    """Parity, the full bar: a seat that was littered, grown and corrupted in place comes out of the reset
    row-set IDENTICAL to a freshly provisioned tenant on EVERY base table (volatile columns normalised), and
    no seeded table is empty."""
    fresh = _provision(dsn, monkeypatch)
    reset = _provision(dsn, monkeypatch)
    try:
        a = _snapshot(dsn, fresh["schema"], fresh["user_id"])
        _litter(dsn, reset["schema"], reset["user_id"])
        before = _snapshot(dsn, reset["schema"], reset["user_id"])
        pre = _diff(a, before)
        assert {"entities", "facts", "rel_types", "rel_type_aliases", "entity_taxonomies", "correction_patterns",
                "climb_state", "episodic_log", "retraction_signals"} <= set(pre), f"litter did not land: {pre}"
        rl = _load_harness(_harness_path(), reset["user_id"], rig, exec_container, qdrant_collection, monkeypatch,
                           name="rl_lr3")
        rl.reset_tenant()
        b = _snapshot(dsn, reset["schema"], reset["user_id"])
        diffs = _diff(a, b)
        print(f"\n[LR3] littered+reset seat vs fresh tenant: tables={len(a)} differing={len(diffs)}\n"
              + (_fmt_diff(a, b, diffs) or "  IDENTICAL on every base table"))
        assert not diffs, f"reset seat ≠ fresh tenant on {diffs} (missing_after_reset, extra_after_reset)"
        seeded = _seeded_tables_by_ast()
        empty = sorted(t for t in seeded & set(b) if not b[t] or b[t] == {("COUNT", 0)})
        assert not empty, f"seeded tables empty after reset: {empty}"
        # the litter is really gone, not merely normalised away
        assert not any("lr_" in str(r) or "CLOBBERED" in str(r) for rows in b.values() for r in rows)
    finally:
        _drop(fresh)
        _drop(reset)


def test_lr3_a_failing_reseed_aborts_loudly_before_anything_is_scored(dsn, rig, exec_container, qdrant_collection,
                                                                     monkeypatch):
    """Two failure shapes, both RAISE out of reset_tenant with the reseed named, and neither proceeds to the
    Qdrant delete / redis flush; through ``_deterministic_pass`` the exception propagates before any ingest.
    (a) the exec door is not there (container absent → docker exec fails);
    (b) the door is there but the seed cannot run (the driver reports ok=false: the db does not exist)."""
    reset = _provision(dsn, monkeypatch)
    try:
        rl = _load_harness(_harness_path(), reset["user_id"], rig, exec_container, qdrant_collection, monkeypatch,
                           name="rl_lr3b")
        assert getattr(rl, "reseed_tenant", None) is not None, "harness has no reseed_tenant — nothing can fail loudly"
        # (a) transport failure
        rl.API_CONTAINER = f"no-such-container-{os.getpid()}"
        calls = _spy_run_cmd(rl)
        with pytest.raises(RuntimeError, match=r"RESEED"):
            rl.reset_tenant()
        assert not any(isinstance(c, list) and c and c[0] == "curl" for c, _ in calls), "proceeded to the Qdrant delete"
        assert rl._flushes == [], "proceeded to the redis flush"
        # (b) seed failure reported by the driver — the door answers, the seed cannot
        rl.API_CONTAINER = exec_container
        rl.PG_DB = f"no_such_db_{os.getpid()}"
        with pytest.raises(RuntimeError, match=r"RESEED.*ok.*False|RESEED.*result=\{"):
            rl.reseed_tenant()
        rl.PG_DB = rig["db"]
        # and the seat, driven the way the run drives it, is never ingested on a failed reseed
        rl.API_CONTAINER = f"no-such-container-{os.getpid()}"
        ingested = []
        rl._ingest_haystack = lambda q, pause: ingested.append(q) or ([], 0)
        monkeypatch.setattr(rl.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match=r"RESEED"):
            rl._deterministic_pass({"question_id": "lr", "haystack_sessions": []}, 0)
        assert ingested == [], "a question was ingested on a seat whose reseed failed"
    finally:
        _drop(reset)
