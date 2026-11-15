"""THE ONTOLOGY WRITE DOOR — who may write it, WHERE it lands, and what it may not overwrite.

WHY THIS EXISTS
    `POST /ontology/rel_types` had three compounding defects, and every one of them was
    INVISIBLE to the suite because the route had NO CALLER anywhere in the repo — no test, no
    tool, no dashboard, no MCP tool, no script. Measured before this file existed:

      1. NO AUTHENTICATION.  `def add_rel_type(req: RelTypeRequest)` — no `Depends`, no key,
         no `user_id`.
      2. NO `search_path` BIND, so it wrote whatever the connection defaulted to. Measured on
         this database: `SHOW search_path` -> `"$user", public`, and PostgreSQL resolves an
         unqualified object in "the first schema in the search path that exists"
         (https://www.postgresql.org/docs/current/ddl-schemas.html) — so every write landed in
         `public.rel_types`, the SEED TEMPLATE every future tenant is provisioned from. The
         codebase's own prose already said otherwise:
         `migrations/130_reconcile_seeded_rel_structural_flags.sql:35` calls a user override a
         "tenant row source='user', set only by POST /ontology/rel_types".
      3. THE TRUST HIERARCHY IN ITS DOCSTRING WAS A DEAD BRANCH. It SELECTed `source`,
         DISCARDED the result, and its only branch was `pass`; the statement below then ran
         `ON CONFLICT (rel_type) DO UPDATE SET source = 'user'` UNCONDITIONALLY. Measured,
         `public.rel_types` holds 50 `builtin` + 16 `wikidata` rows and ZERO of anything else —
         so every row the endpoint could reach in the template was EXACTLY the class its
         docstring promised could not be overwritten. Meanwhile `src/re_embedder/embedder.py`
         (~:9003) justifies UNGATING novel rel_type approval on the claim that "the trust
         hierarchy user > wikidata > engine > builtin is enforced at /ontology/rel_types".
         It was not.

WHICH PINS GO RED AGAINST THE ORIGINAL CODE — stated explicitly, because a pin that could not
have caught the bug is decoration:

  * `test_a_tenant_write_never_touches_the_public_seed_template`  — RED before. The original
    bound no schema, so this novel rel_type appeared in `public.rel_types` every time.
  * `test_a_builtin_sourced_rel_type_is_refused_not_overwritten`  — RED before. The original
    returned `{"status": "ok"}` and rewrote the row's label with `source='user'`.
  * `test_a_wikidata_sourced_rel_type_is_refused_not_overwritten` — RED before, same reason.
  * `test_the_public_seed_lane_is_refused_without_the_operator_credential` — RED before. The
    original had no credential check of any kind and wrote `public` unconditionally.
  * `test_a_request_naming_no_tenant_is_refused_rather_than_defaulting_to_public` — RED
    before: with no `user_id` concept at all, the original's ONLY behaviour was the `public`
    write this now refuses.

DB-BOUND BY NECESSITY. The load-bearing claims are "the write landed in schema X" and "the
protected row is byte-unchanged"; a monkeypatched cursor cannot prove either — it would pin
the mock. These tests create and drop their OWN throwaway schema and never touch a real
tenant. The one test that does write `public` uses a uuid-suffixed rel_type and deletes it in
a `finally`.
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_DSN"),
    reason="the ontology write door is DB-bound by design — set POSTGRES_DSN",
)

# Only the columns this endpoint writes or reads. Deliberately NOT the full template DDL:
# a minimal table proves the handler does not depend on anything it was not given.
_REL_TYPES_DDL = """
CREATE TABLE {s}.rel_types (
    rel_type TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    wikidata_pid TEXT,
    head_types TEXT[],
    tail_types TEXT[],
    engine_generated BOOLEAN NOT NULL DEFAULT false,
    confidence FLOAT NOT NULL DEFAULT 1.0,
    correction_behavior TEXT,
    source TEXT,
    is_symmetric BOOLEAN DEFAULT false,
    inverse_rel_type TEXT,
    is_hierarchy_rel BOOLEAN DEFAULT false
);
"""


def _dsn():
    return os.environ["POSTGRES_DSN"]


class _Tenant:
    """A throwaway tenant schema carrying ONLY `rel_types`, pre-seeded with one row of each
    trust level so the hierarchy can be exercised without inventing rows mid-test."""

    def __init__(self):
        import psycopg2
        from src.provisioning.schema_manager import derive_user_slug_from_uuid
        self.user_id = str(uuid.uuid4())
        self.schema = "faultline_" + derive_user_slug_from_uuid(self.user_id)
        self._conn = psycopg2.connect(_dsn())
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {self.schema}")
            cur.execute(_REL_TYPES_DDL.format(s=self.schema))
            for rt, label, source in (
                ("has_pet", "SEEDED BUILTIN LABEL", "builtin"),
                ("spouse", "SEEDED WIKIDATA LABEL", "wikidata"),
                ("frobnicates", "GROWN ENGINE LABEL", "engine"),
                ("gleeps", "LEARNED VIA EXPAND", "expand"),
                ("already_mine", "AN EARLIER USER ASSERTION", "user"),
            ):
                cur.execute(
                    f"INSERT INTO {self.schema}.rel_types (rel_type, label, source) "
                    "VALUES (%s,%s,%s)", (rt, label, source))

    def row(self, rel_type):
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT label, source FROM {self.schema}.rel_types "
                        "WHERE rel_type = %s", (rel_type,))
            return cur.fetchone()

    def drop(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        finally:
            self._conn.close()


def _public_row(rel_type):
    import psycopg2
    with psycopg2.connect(_dsn()) as c:
        with c.cursor() as cur:
            cur.execute("SELECT label, source FROM public.rel_types WHERE rel_type = %s",
                        (rel_type,))
            return cur.fetchone()


def _public_delete(rel_type):
    import psycopg2
    with psycopg2.connect(_dsn()) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DELETE FROM public.rel_types WHERE rel_type = %s", (rel_type,))


@pytest.fixture
def tenant():
    t = _Tenant()
    try:
        yield t
    finally:
        t.drop()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from src.api.main import app
    # No context manager: the lifespan pulls models this door never touches.
    return TestClient(app)


@pytest.fixture(autouse=True)
def _deterministic_operator_env(monkeypatch):
    """Both accepted key names are cleared for EVERY test, so a key that happens to be set in
    the developer's real environment can never make a fail-closed assertion pass by accident.
    Each test that wants a credential sets exactly one."""
    monkeypatch.delenv("SAAS_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)


def _body(rel_type, tenant=None, **kw):
    b = {"rel_type": rel_type, "label": "Asserted By A User"}
    if tenant is not None:
        b["user_id"] = tenant.user_id
    b.update(kw)
    return b


# ── 1. AUTH — the seed-template lane ────────────────────────────────────────────────────

def test_the_public_seed_lane_is_refused_without_the_operator_credential(client):
    """RED against the original: it had no credential check at all and wrote `public`
    unconditionally. `public.rel_types` is the template every future tenant inherits, so an
    unauthenticated write here propagates forward — OWASP API5:2023 asks that the mechanism
    "deny all access by default"."""
    novel = f"pin_unauth_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, scope="public"))
    assert r.status_code == 403, r.text
    assert _public_row(novel) is None, "a refused request must not have written anything"


def test_an_unset_operator_key_refuses_rather_than_opening_the_door(client):
    """THE fail-closed direction that matters: with NO key configured, a well-formed bearer
    must still be refused. The opposite convention ("no key means open") is exactly how this
    endpoint came to be unauthenticated."""
    novel = f"pin_unsetkey_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, scope="public"),
                    headers={"Authorization": "Bearer anything-at-all"})
    assert r.status_code == 403, r.text
    assert _public_row(novel) is None


@pytest.mark.parametrize("header", [
    "Bearer wrong-key",           # right scheme, wrong secret
    "correct-horse-battery",      # no scheme at all
    "Basic Y29ycmVjdA==",         # wrong scheme
    "Bearer ",                    # empty presented token
])
def test_a_malformed_or_wrong_credential_is_refused(client, monkeypatch, header):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse-battery")
    novel = f"pin_badcred_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, scope="public"),
                    headers={"Authorization": header})
    assert r.status_code == 403, r.text
    assert _public_row(novel) is None


@pytest.mark.parametrize("env_name", ["SAAS_OPERATOR_KEY", "ADMIN_API_KEY"])
def test_either_accepted_operator_key_name_authorises_the_seed_lane(client, monkeypatch,
                                                                    env_name):
    """Both names are accepted deliberately: `SAAS_OPERATOR_KEY` is the operator credential on
    the SaaS line, `ADMIN_API_KEY` is what `main.py` already uses for its own admin route, and
    an open-core deployment has no `saas/` package to supply the former.

    This is also the "an AUTHORISED request succeeds" pin AND the "the schema actually written
    is the intended one" pin for the operator lane: the row must appear in `public`."""
    monkeypatch.setenv(env_name, "the-operator-secret")
    novel = f"pin_pubok_{uuid.uuid4().hex[:10]}"
    try:
        r = client.post("/ontology/rel_types", json=_body(novel, scope="public"),
                        headers={"Authorization": "Bearer the-operator-secret"})
        assert r.status_code == 200, r.text
        assert r.json()["scope"] == "public"
        assert r.json()["schema"] == "public"
        assert _public_row(novel) == ("Asserted By A User", "user")
    finally:
        _public_delete(novel)


# ── 2. SCHEMA BINDING — where the write actually lands ──────────────────────────────────

def test_a_tenant_write_never_touches_the_public_seed_template(client, tenant):
    """THE pin that would have caught defect #2. RED against the original: with no
    `SET search_path`, the connection defaulted to `"$user", public` and this novel rel_type
    appeared in `public.rel_types` on every call."""
    novel = f"pin_tenant_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, tenant))
    assert r.status_code == 200, r.text
    assert r.json()["schema"] == tenant.schema
    assert tenant.row(novel) == ("Asserted By A User", "user"), "tenant row must exist"
    assert _public_row(novel) is None, "THE SEED TEMPLATE MUST BE UNTOUCHED"


def test_a_request_naming_no_tenant_is_refused_rather_than_defaulting_to_public(client):
    """RED against the original, whose ONLY behaviour was this write. A missing `user_id` must
    be an honest refusal — never a silent fall-through to the template."""
    novel = f"pin_notenant_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel))
    assert r.status_code == 400, r.text
    assert _public_row(novel) is None


def test_an_unprovisioned_tenant_is_an_honest_miss_not_a_seed_write(client):
    """The tenant bind is WITHOUT `public`, so an unprovisioned schema has no reachable
    `rel_types` at all. That must surface as a 400 naming the schema — not a 500, and above
    all not a fall-through onto the template."""
    novel = f"pin_ghost_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, user_id=str(uuid.uuid4())))
    assert r.status_code == 400, r.text
    assert "rel_types" in r.text
    assert _public_row(novel) is None


def test_a_tenant_id_that_derives_no_schema_is_refused(client):
    novel = f"pin_badtenant_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, user_id="anonymous"))
    assert r.status_code == 400, r.text
    assert _public_row(novel) is None


@pytest.mark.parametrize("hostile_uid", [
    "public",                            # name the template directly
    "'; DROP SCHEMA public CASCADE; --",  # steer the interpolated SET search_path
    "../public",
    "faultline_x, public",               # append `public` back onto the bound path
])
def test_the_tenant_lane_cannot_be_steered_at_the_public_schema(client, hostile_uid):
    """`schema_name` reaches an interpolated `SET search_path TO {schema_name}`, so the
    derivation is load-bearing, not cosmetic. `derive_user_slug_from_uuid` gates on a strict
    lowercase-UUID regex and RAISES on anything else (`_derive_tenant_schema` catches -> None
    -> 400), and `derive_schema_name` additionally collapses every non-alphanumeric character
    to `_`. Belt AND suspenders — pinned here so a future relaxation of either one goes red at
    this door rather than in production."""
    novel = f"pin_steer_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, user_id=hostile_uid))
    assert r.status_code == 400, r.text
    assert _public_row(novel) is None


# ── 3. TRUST HIERARCHY — user > wikidata > engine > builtin ─────────────────────────────

@pytest.mark.parametrize("rel_type,seeded_source,seeded_label", [
    ("has_pet", "builtin", "SEEDED BUILTIN LABEL"),
    ("spouse", "wikidata", "SEEDED WIKIDATA LABEL"),
])
def test_a_curated_rel_type_is_refused_not_overwritten(client, tenant, rel_type,
                                                       seeded_source, seeded_label):
    """RED against the original: the `SELECT source` result was discarded, the only branch was
    `pass`, and `ON CONFLICT DO UPDATE SET source='user'` then ran unconditionally — so the
    original returned 200 and rewrote both label and source.

    Two assertions, not one: the STATUS proves the refusal, and the ROW proves the refusal was
    real rather than a 409 returned after the write already landed."""
    r = client.post("/ontology/rel_types", json=_body(rel_type, tenant))
    assert r.status_code == 409, r.text
    assert seeded_source in r.text
    assert tenant.row(rel_type) == (seeded_label, seeded_source), "row must be byte-unchanged"


# Kept as separately-named entry points so a failure names the source that regressed.
def test_a_builtin_sourced_rel_type_is_refused_not_overwritten(client, tenant):
    r = client.post("/ontology/rel_types", json=_body("has_pet", tenant))
    assert r.status_code == 409, r.text
    assert tenant.row("has_pet") == ("SEEDED BUILTIN LABEL", "builtin")


def test_a_wikidata_sourced_rel_type_is_refused_not_overwritten(client, tenant):
    r = client.post("/ontology/rel_types", json=_body("spouse", tenant))
    assert r.status_code == 409, r.text
    assert tenant.row("spouse") == ("SEEDED WIKIDATA LABEL", "wikidata")


@pytest.mark.parametrize("rel_type", ["frobnicates", "gleeps", "already_mine"])
def test_user_outranks_engine_expand_and_a_prior_user_assertion(client, tenant, rel_type):
    """PRECISION, not a blanket refusal. `user` outranks `engine`; `expand` is the `/learn`
    lane (LLM-learned, engine-grade) and is ranked with it deliberately rather than left
    undefined by the four-name hierarchy; and a user may of course update their own row.
    Without this pin, "refuse everything" would score identically to the real hierarchy."""
    r = client.post("/ontology/rel_types", json=_body(rel_type, tenant))
    assert r.status_code == 200, r.text
    assert tenant.row(rel_type) == ("Asserted By A User", "user")


def test_the_refusal_survives_ablating_the_python_precheck(client, tenant, monkeypatch):
    """ABLATION of `_PROTECTED_REL_TYPE_SOURCES`, and it proves something the happy path
    cannot: with the Python pre-`SELECT` guard emptied, the refusal must STILL hold, because
    the binding enforcement is the `WHERE ... NOT IN ('wikidata','builtin')` on the
    `DO UPDATE`. A guard that lived only in the pre-`SELECT` would be a check-then-act race."""
    from src.api import main as m
    monkeypatch.setattr(m, "_PROTECTED_REL_TYPE_SOURCES", ())
    r = client.post("/ontology/rel_types", json=_body("has_pet", tenant))
    assert r.status_code == 409, r.text
    assert tenant.row("has_pet") == ("SEEDED BUILTIN LABEL", "builtin")


# ── 4. DEFAULTS pinned separately from behaviour ────────────────────────────────────────

def test_scope_defaults_to_tenant_never_to_public(client, tenant):
    """The default is the whole safety property: an omitted `scope` must mean the tenant lane.
    Pinned on the MODEL's default and then on the observed behaviour, so a change to either
    alone goes red."""
    from src.api.models import RelTypeRequest
    assert RelTypeRequest(rel_type="x", label="X").scope is None
    novel = f"pin_default_{uuid.uuid4().hex[:10]}"
    body = _body(novel, tenant)
    assert "scope" not in body
    r = client.post("/ontology/rel_types", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "tenant"
    assert _public_row(novel) is None


def test_user_id_defaults_to_absent_so_a_tenant_must_be_named(client):
    from src.api.models import RelTypeRequest
    assert RelTypeRequest(rel_type="x", label="X").user_id is None


def test_the_protected_source_set_is_exactly_the_two_curated_sources():
    """Pinned as a DEFAULT, apart from the behaviour above. `engine` and `expand` are
    deliberately absent — including them would make the endpoint unable to correct the
    engine's own guesses, which is the correction path the growth ruling depends on."""
    from src.api import main as m
    assert m._PROTECTED_REL_TYPE_SOURCES == ("wikidata", "builtin")


@pytest.mark.parametrize("scope", ["", "  ", "PUBLIC", "tenant"])
def test_scope_is_case_and_whitespace_normalised_not_silently_widened(client, tenant, scope):
    """`PUBLIC` must reach the CREDENTIAL gate (403 with no key), never be treated as an
    unknown value that quietly degrades to something permissive; blank must mean tenant."""
    novel = f"pin_scopenorm_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, tenant, scope=scope))
    if scope.strip().lower() == "public":
        assert r.status_code == 403, r.text
        assert _public_row(novel) is None
    else:
        assert r.status_code == 200, r.text
        assert r.json()["scope"] == "tenant"


def test_an_unrecognised_scope_is_refused_not_widened(client, tenant):
    novel = f"pin_badscope_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types",
                    json=_body(novel, tenant, scope="../public"))
    assert r.status_code == 400, r.text
    assert _public_row(novel) is None
    assert tenant.row(novel) is None


# ── 5. THE HARD LINE — this door writes STRUCTURE, never a memory ───────────────────────

def test_the_ontology_door_cannot_reach_a_memory_row(client, tenant):
    """The throwaway schema has NO `facts` / `staged_facts` / `entities` table. A successful
    write against it proves the handler touches `rel_types` and nothing else — a shelf, never
    a memory."""
    novel = f"pin_hardline_{uuid.uuid4().hex[:10]}"
    r = client.post("/ontology/rel_types", json=_body(novel, tenant))
    assert r.status_code == 200, r.text
