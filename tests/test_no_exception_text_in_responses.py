"""IE2 — no exception text in a response body; every unexpected failure carries a correlation id.

Gauntlet: DEV/review/gauntlet/codeql-info-exposure (CodeQL py/stack-trace-exposure ×33).
Bar IE2: ONE error seam (src/api/errors.py, saas door saas/errors.py) used by every route that
used to return ``str(e)`` / ``repr(e)`` / ``f"…{exc}"``; the helper logs the full exception with a
correlation id that the body also carries.

Evidence: OWASP ASVS 4.0 V7.4.1 (generic message + unique ID for unexpected errors), V14.4 /
CWE-209 (no stack detail in responses). Each route below is driven through the REAL FastAPI app
(router mounted as production mounts it) with a dependency monkeypatched to raise
``RuntimeError("SECRET-CANARY-XYZ")``; the body must NOT carry the canary, MUST carry the
authored message + a correlation id, and the log MUST carry both the canary and that id.

Run:  FLTEST_LEDGER=... python3 tools/fltest.py --bug IE5 --test tests/test_no_exception_text_in_responses.py
"""

from __future__ import annotations

import logging
import os
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
CANARY = "SECRET-CANARY-XYZ"
_CID = re.compile(r"^[0-9a-f]{12}$")
_REF = re.compile(r"\(ref ([0-9a-f]{12})\)")


class _Boom(RuntimeError):
    pass


def _boom(*_a, **_k):
    raise _Boom(CANARY)


def _assert_generic(body_text: str, what: str, caplog):
    """The body carries the authored phrase + a correlation id and NOT the canary; the log carries
    the canary under the same id."""
    assert CANARY not in body_text, body_text
    assert what in body_text, body_text
    m = _REF.search(body_text)
    cid = None
    if m:
        cid = m.group(1)
    else:
        m2 = re.search(r'"correlation_id":\s*"([0-9a-f]{12})"', body_text)
        assert m2, body_text
        cid = m2.group(1)
    assert _CID.match(cid)
    recs = [r for r in caplog.records if r.name == "faultline.errors"]
    assert recs, "the seam wrote no log record"
    joined = "\n".join(r.getMessage() for r in recs)
    assert cid in joined and CANARY in joined, joined[:400]
    return cid


# ── the seam itself ─────────────────────────────────────────────────────────────────

def test_seam_unexpected_vs_refusal(caplog):
    from src.api import errors as E
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    try:
        raise _Boom(CANARY)
    except Exception as exc:
        d = E.public_error(exc, where="t.unit", what="thing failed")
        s = E.public_detail(exc, where="t.unit", what="thing failed")
    assert d["error"] == "thing failed" and _CID.match(d["correlation_id"])
    assert s.startswith("thing failed (ref ") and CANARY not in s
    # a refusal passes its AUTHORED sentence and is not an incident
    n_before = len(caplog.records)

    class _Refuse(E.PublicRefusal, ValueError):
        pass
    try:
        raise _Refuse("bucket must be an integer")
    except Exception as exc:
        assert E.public_error(exc, where="t") == {"error": "bucket must be an integer"}
        assert E.public_detail(exc, where="t") == "bucket must be an integer"
    assert len(caplog.records) == n_before


def test_seam_log_line_is_redacted_when_saas_redactor_installed(caplog):
    """The correlation-id CRIT line itself goes through the saas redactor (ASVS V7.1.1)."""
    import saas.errors  # noqa: F401 — installs saas/redact.py into the engine seam
    from src.api import errors as E
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    try:
        raise _Boom("Client error for url 'https://x.example/v1?api_key=abcdefghijklmnop'")
    except Exception as exc:
        E.public_detail(exc, where="t.redact")
    joined = "\n".join(r.getMessage() for r in caplog.records if r.name == "faultline.errors")
    assert "abcdefghijklmnop" not in joined and "[masked]" in joined


def test_registered_refusals_pass_authored_sentences():
    """EgressBlocked (frozen module, registered), SignatureRequired, binary-intake refusals."""
    from saas import errors as SE
    from saas.entitlements import SignatureRequired
    from saas.llm_config import EgressBlocked
    from src.ingest.binary_intake import ArtefactBomb, UnsupportedMediaType
    for exc in (EgressBlocked("scheme 'ftp' not allowed (http or https only)"),
                SignatureRequired("api_addon_on requires a typed-name signature"),
                UnsupportedMediaType("looked like text/x-shellscript"),
                ArtefactBomb("page count 100000 exceeds the cap")):
        assert SE.is_refusal(exc)
        assert SE.public_detail(exc, where="t") == exc.args[0]


def test_provider_detail_redacts_the_key_this_call_sent():
    from saas import errors as SE
    key = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    exc = _Boom(f"Incorrect API key provided: {key} for url 'https://x/v1?api-key={key}'")
    out = SE.provider_detail(exc, secrets=[key], what="call failed")
    assert key not in out and out.startswith("call failed: _Boom")


# ── billing dashboard: six operator routes ──────────────────────────────────────────

OP_KEY = "op-ie-secret-key"


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "ie-test-secret-0123456789abcdef")
    yield


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


@pytest.mark.parametrize("path", ["/admin/billing/state", "/admin/billing/mrr",
                                  "/admin/billing/dunning", "/admin/billing/lifecycle",
                                  "/admin/billing/refunds", "/admin/billing/email-controls"])
def test_billing_dashboard_routes(saas_on, monkeypatch, caplog, path):
    from saas import billing_dashboard_routes as bdr
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(bdr.db, "connection", _boom)
    app = FastAPI()
    app.include_router(bdr.router)
    r = TestClient(app).get(path, headers=_op())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] == "control-plane unavailable"
    _assert_generic(r.text, "control-plane unavailable", caplog)


# ── operator console: subscribers + pricing ──────────────────────────────────────────

def test_admin_subscribers_snapshot(saas_on, monkeypatch, caplog):
    from saas import admin, subscribers
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(subscribers.db, "connection", _boom)
    app = FastAPI()
    app.include_router(admin.router)
    r = TestClient(app).get("/admin/subscribers", headers=_op())
    assert r.status_code == 200, r.text
    assert r.json()["error"] == "control-plane unavailable"
    _assert_generic(r.text, "control-plane unavailable", caplog)


def test_admin_pricing_account_kind_catalog(saas_on, monkeypatch, caplog):
    from saas import admin, entitlements
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(entitlements, "account_kind_catalog", _boom)
    app = FastAPI()
    app.include_router(admin.router)
    r = TestClient(app).get("/admin/pricing", headers=_op())
    assert r.status_code == 200, r.text
    assert r.json()["account_kinds"] == []
    assert "account-kind catalog unreadable (ref " in r.json()["account_kinds_error"]
    _assert_generic(r.text, "account-kind catalog unreadable", caplog)


def test_admin_payment_and_smtp_config_refusals_keep_their_sentence(saas_on, monkeypatch):
    """A typed ConfigRefused passes verbatim; an unexpected ValueError does not."""
    from saas import admin, payment_config, smtp_config
    app = FastAPI()
    app.include_router(admin.router)
    c = TestClient(app)
    monkeypatch.setattr(payment_config, "upsert_config",
                        lambda *a, **k: (_ for _ in ()).throw(payment_config.ConfigRefused("invalid mode 'x'")))
    r = c.put("/admin/payment-config", headers=_op(), json={"mode": "x"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid mode 'x'", r.text
    monkeypatch.setattr(smtp_config, "upsert_config",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError(CANARY)))
    r = c.put("/admin/smtp-config", headers=_op(), json={"port": 25})
    assert r.status_code == 400 and CANARY not in r.text
    assert r.json()["detail"].startswith("invalid SMTP configuration (ref ")


# ── entitlements / seat sync reasons ────────────────────────────────────────────────

def test_entitlement_ledger_reason_is_generic(monkeypatch, caplog):
    from saas import entitlements as ent
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(ent.db, "connection", _boom)
    out = ent.record_api_addon_change("00000000-0000-4000-8000-000000000001", True,
                                      actor="t", signature="Tester")
    assert out["ok"] is False and out["status_code"] == 500
    assert CANARY not in out["reason"] and out["reason"].startswith("ledger error (ref ")
    _assert_generic(out["reason"], "ledger error", caplog)


def test_signature_required_reason_is_the_authored_sentence(monkeypatch):
    """The ledger's own refusal (raised inside ``_write_ledger`` on an open cursor) reaches the
    caller as its AUTHORED sentence through the seam — a 400 with the instruction, not 'internal
    error (ref …)'. Driven with a cursor stand-in so the refusal fires before any SQL."""
    from saas import entitlements as ent

    class _Cur:
        def execute(self, *_a, **_k):  # pragma: no cover — must not be reached
            raise AssertionError("SQL ran before the signature gate")

    with pytest.raises(ent.SignatureRequired) as ei:
        ent._write_ledger(_Cur(), "00000000-0000-4000-8000-000000000001", ent.BILLABLE_ACTIONS[0],
                          None, 1, 1.0, 1.0, 12, "t", "note", signature="")
    exc = ei.value
    assert isinstance(exc, ent._errors.PublicRefusal)
    assert ent._errors.public_detail(exc, where="t") == exc.public_message
    assert "requires a typed-name signature" in exc.public_message


# ── MCP HTTP door: prompts/get ──────────────────────────────────────────────────────

@pytest.fixture
def mcp_client(monkeypatch):
    """Open-core MCP door, the environment tests/mcp/conftest.py states: SaaS off, auth off,
    no pinned default tenant (the C5 startup guard refuses SAAS_MODE + FAULTLINE_USER_ID)."""
    import src.mcp.server as _mcp
    import src.mcp.http_server as h
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("FAULTLINE_USER_ID", raising=False)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setattr(h, "MCP_API_KEY", "")
    monkeypatch.setattr(_mcp, "_FAULTLINE_URL_DETECTED", True)
    with TestClient(h.app) as c:
        yield c


def test_mcp_prompts_get_execution_failure_is_generic(mcp_client, monkeypatch, caplog):
    from src.mcp import prompts as P
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    fn_key = P.PROMPTS[0]
    monkeypatch.setitem(fn_key, "fn", _boom)
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                                      "params": {"name": fn_key["name"], "arguments": {}}})
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == -32603
    _assert_generic(err["message"], "Prompt execution failed", caplog)


def test_mcp_prompts_get_missing_argument_names_the_argument_not_the_signature(mcp_client, monkeypatch):
    from src.mcp import prompts as P

    def needs_topic(topic):  # a prompt with a REQUIRED argument
        return f"about {topic}"
    monkeypatch.setitem(P.PROMPTS[0], "fn", needs_topic)
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
                                      "params": {"name": P.PROMPTS[0]["name"], "arguments": {}}})
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert r.json()["error"]["code"] == -32602
    assert "missing required argument(s): topic" in msg
    assert "needs_topic" not in msg and "positional" not in msg   # no Python signature text


# ── engine routes (src/api/main.py) ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine_client():
    from src.api.main import app
    return TestClient(app)


def test_engine_provisioning_status(engine_client, monkeypatch, caplog):
    import src.provisioning.provisioning_status as ps
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(ps, "check_provisioning_status", _boom)
    monkeypatch.setattr(ps, "ensure_user_provisioned", _boom)
    r = engine_client.get("/provisioning/status",
                          params={"user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "error" and r.json()["error"] == "Failed to check status"
    _assert_generic(r.text, "Failed to check status", caplog)


@pytest.mark.parametrize("path,payload,what", [
    ("/structure/deactivate-cue",
     {"user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "cue": "x", "category": "naming_verb"},
     "cue not retired"),
    ("/documents/retry",
     {"user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "document_id": 1},
     "retry failed"),
])
def test_engine_db_backed_routes(engine_client, monkeypatch, caplog, path, payload, what):
    from src.api import main as M
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(M.psycopg2, "connect", _boom)
    r = engine_client.post(path, json=payload)
    assert r.status_code == 200, r.text
    _assert_generic(r.text, what, caplog)


# ── the IE2 bar itself: an AST census, not a grep ─────────────────────────────────

def _load_census():
    """tests/ is not a package (gitignored, no __init__); load the census module by path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ie2_census", ROOT / "tests" / "ie2_census.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def test_ast_census_no_exception_rooted_expression_reaches_a_response():
    """Round-2 replacement for the same-line regex census (the round-1 critic reached exception
    text through a helper's except-dict, a multi-line ``QueryResponse(error=…)`` constructor,
    and a helper's ``last_detail`` — none visible to a line window). ``tests/ie2_census.py``
    walks every function in saas/ and ALL of src/: every ``return`` value, every
    ``HTTPException(...)``, every ``*Response(...)`` / ``_jsonrpc_error(...)`` call, every
    ``yield`` (a StreamingResponse chunk) and every DB-write parameter (a persisted error column
    a status route renders — round 3) is a sink;
    a name bound by ``except … as e`` (live in its handler), any name assigned from it inside
    the handler (live for the rest of the function), ``.args``/``__traceback__`` and
    ``traceback.format_exc()`` are sources; the seam, the codebase's own redactors and
    authored attribute reads are the only sanitizers (named + justified in the module)."""
    C = _load_census()
    rows = C.census_tree(ROOT)
    found = {(rel, fn, kind, txt) for (rel, _ln, fn, kind, txt) in rows}
    new_sites = found - C.KNOWN_RESIDUALS
    gone = C.KNOWN_RESIDUALS - found
    assert not new_sites, "exception-rooted expression reaches a body/row:\n" + "\n".join(map(str, sorted(new_sites)))
    assert not gone, "a KNOWN_RESIDUAL is fixed — remove it from tests/ie2_census.py:\n" + "\n".join(map(str, sorted(gone)))


def test_ast_census_catches_the_shapes_the_regex_missed(tmp_path):
    """The census must go RED on each shape the round-1 critic drove green through the regex."""
    C = _load_census()
    src = '''
def helper():
    try:
        work()
    except Exception as e:
        return {"status": "error", "error_message": f"Failed: {str(e)}"}   # helper except-dict

def route():
    try:
        work()
    except Exception as e:
        return QueryResponse(
            anchor="",
            error=str(e),                                                  # multi-line ctor kwarg
        )

def probe():
    last_detail = ""
    try:
        work()
    except Exception as exc:
        last_detail = f"{type(exc).__name__}: {str(exc)[:160]}"          # assigned, returned later
    return {"detail": last_detail}

def tb():
    import traceback
    try:
        work()
    except Exception:
        return {"trace": traceback.format_exc()}

def args():
    try:
        work()
    except Exception as e:
        raise HTTPException(status_code=500, detail=e.args[0])

def store():
    try:
        work()
    except Exception as e:
        cur.execute("UPDATE documents SET error = %s WHERE id = %s", (str(e)[:500], 1))   # persisted row

def stream():
    try:
        work()
    except Exception as e:
        yield "data: " + str(e)                                                      # StreamingResponse chunk

def handoff():
    try:
        work()
    except Exception as e:
        return ("failed", e)                                                         # object, unformatted

def clean():
    try:
        work()
    except Exception as e:
        return {"error": public_detail(e, where="x"), "kind": type(e).__name__, "code": getattr(e, "pgcode", None)}
'''
    f = tmp_path / "m.py"; f.write_text(src, encoding="utf-8")
    hits = C.census(f)
    flagged = {fn for (_ln, fn, _k, _t) in hits}
    assert flagged == {"helper", "route", "probe", "tb", "args", "store", "stream"}, hits


# ── the round-1 critic's four driven leaks ──────────────────────────────────────────

def test_provisioning_status_helper_dict_is_a_public_shape(engine_client, monkeypatch, caplog):
    """The route returns check_provisioning_status's own except-dict verbatim — so THAT dict
    must be a public shape (round-1 patched only the route's own arm)."""
    import src.provisioning.provisioning_status as ps
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(ps, "get_postgres_connection", _boom)
    r = engine_client.get("/provisioning/status",
                          params={"user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "error" and body["error_message"] == "Failed to check status"
    assert CANARY not in r.text and _CID.match(body["correlation_id"])
    _assert_generic(r.text, "Failed to check status", caplog)


def test_mcp_tools_call_catch_all_is_generic(mcp_client, monkeypatch, caplog):
    import src.mcp.server as _mcp
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setitem(_mcp.TOOL_DISPATCH, "recall_memory", _boom)
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                      "params": {"name": "recall_memory",
                                                 "arguments": {"query": "x", "user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}}})
    assert r.status_code == 200, r.text
    assert CANARY not in r.text
    text = r.json()["result"]["content"][0]["text"]
    body = __import__("json").loads(text)
    assert body["error"] == "Unexpected error" and _CID.match(body["correlation_id"])
    _assert_generic(text, "Unexpected error", caplog)


def test_query_response_error_field_is_generic(engine_client, monkeypatch, caplog):
    """``_query_impl`` ends in ``return QueryResponse(..., error=str(e))`` — a multi-line
    constructor kwarg the regex census never saw."""
    from src.api import main as M
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    monkeypatch.setattr(M.psycopg2, "connect", _boom)

    async def _ready(*_a, **_k):   # the provisioning await before _query_impl — not under test
        return None
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    r = engine_client.post("/query", json={"text": "what is my name",
                                           "user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"})
    assert r.status_code == 200, r.text
    assert CANARY not in r.text, r.text
    err = r.json()["error"]
    m = _REF.search(err)
    assert m and err.startswith(("query failed", "schema context failed")), err
    # the outer handler catches whatever the inner rollback path raised (here an
    # UnboundLocalError downstream of the canary), so pin the id ↔ log link, not the canary text
    joined = "\n".join(rec.getMessage() for rec in caplog.records if rec.name == "faultline.errors")
    assert m.group(1) in joined and "traceback=" in joined


def test_probe_reframe_latency_detail_never_carries_the_key(monkeypatch, caplog):
    """The calibration half of Test & calibrate: ``probe_reframe_latency`` used to put raw
    ``str(exc)`` in ``detail`` → ``result["calibration"]`` — the critic read the tenant's whole
    key back out of the three recalibrate routes. Since the CQ-hardening contract change the
    DETAIL is curated (class + authored reason; no key can be in it) and the full redacted
    sentence lands in the ``faultline.errors`` log — this pins both channels."""
    import logging as _logging
    from saas import llm_admin as la
    key = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    cfg = {"backend_type": "openai", "base_url": "https://llm.example/v1", "model": "m",
           "api_key": key}

    def _raise(**_k):
        raise RuntimeError(f"Client error '401' for url 'https://llm.example/v1?api-key={key}'")
    import src.api.llm_calls as lc
    monkeypatch.setattr(lc, "call_llm_with_retry_sync", _raise)
    monkeypatch.setattr(la, "profile_timeout", lambda *a, **k: 5.0, raising=False)
    with caplog.at_level(_logging.WARNING, logger="faultline.errors"):
        out = la.probe_reframe_latency("00000000-0000-4000-8000-000000000001", cfg, samples=2)
    assert out["ok"] is False
    assert key not in out["detail"], out["detail"]
    assert out["detail"].startswith("call failed: RuntimeError: "), out["detail"]
    # the full redacted sentence is server-side, with the key masked
    assert "[redacted]" in caplog.text or "[masked]" in caplog.text, caplog.text
    assert key not in caplog.text, caplog.text
    # and the whole Test & calibrate result the routes render
    probe = la._probe_extraction("00000000-0000-4000-8000-000000000001", cfg)
    assert key not in probe["detail"], probe["detail"]
    assert probe["detail"].startswith("call failed: "), probe["detail"]


# ── round 3: PERSISTED error columns are bodies (the critic drove both on the real DB) ──

_DSN = os.environ.get("POSTGRES_DSN", "")
_DB_PASSWORD_SHAPED = "postgresql://svc:hunter2-SECRET@db.internal:5432/x"


@pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN unset — the real create_user_schema path needs the throwaway DB")
def test_real_create_user_schema_failure_row_is_public_via_provisioning_status(engine_client, monkeypatch, caplog):
    """The REAL create_user_schema against the throwaway DB: a failure inside it used to write
    ``str(e)[:500]`` into public.user_provisioning.error_message; check_provisioning_status
    returns the row; GET /provisioning/status returns it verbatim — the round-2 critic read the
    canary AND a DSN password back out of the body. Now the row holds the public shape."""
    import uuid
    import psycopg2
    from src.provisioning import schema_manager as sm
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    user_id = str(uuid.uuid4())
    slug = sm.derive_user_slug_from_uuid(user_id)
    schema = sm.derive_schema_name(slug)
    calls = {"n": 0}

    class _FlakyConn(psycopg2.extensions.connection):
        """The FIRST cursor the provisioner opens raises the canary (outside every inner try, so
        the outer except writes the row); every later cursor — including the one that records
        the failure — is real. Armed by the test right before create_user_schema."""
        armed = False

        def cursor(self, *a, **k):
            if self.armed:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError(f"{CANARY} while binding {_DB_PASSWORD_SHAPED}")
            return super().cursor(*a, **k)

    conn = psycopg2.connect(_DSN, connection_factory=_FlakyConn)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES (%s, %s, %s, %s)",
                        (user_id, f"ie3-{user_id[:8]}@example.com", "ie3 probe", slug))
            cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status) VALUES (%s, %s, 'provisioning')",
                        (user_id, schema))
        conn.commit()
        conn.armed = True
        _schema, status = sm.create_user_schema(user_id, slug, conn)
        conn.armed = False
        assert status.startswith("Error: provisioning failed (ref "), status
        assert CANARY not in status and "hunter2" not in status
        with conn.cursor() as cur:
            cur.execute("SELECT status, error_message FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        assert row[0] == "error"
        assert CANARY not in row[1] and "hunter2" not in row[1], row[1]
        m = _REF.search(row[1]); assert m, row[1]
        # the route renders that row
        r = engine_client.get("/provisioning/status", params={"user_id": user_id})
        assert r.status_code == 200, r.text
        assert CANARY not in r.text and "hunter2" not in r.text, r.text
        assert r.json()["status"] == "error" and m.group(1) in r.json()["error_message"]
        joined = "\n".join(rec.getMessage() for rec in caplog.records if rec.name == "faultline.errors")
        assert m.group(1) in joined and CANARY in joined
    finally:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        finally:
            conn.close()


def test_document_lane_writers_persist_the_public_shape(monkeypatch, caplog):
    """The document lane's three persisted-error writers (per-chunk reason, dead-letter reason,
    per-document error) — driven at the writer with a canary + DSN-shaped password; what they
    hand to the row (and GET /documents/status / the document_status tool render) carries neither."""
    from src.re_embedder import embedder as E
    caplog.set_level(logging.CRITICAL, logger="faultline.errors")
    # NOT a RuntimeError: that class is the drain's freeze signal and is re-raised by design
    boom = ValueError(f"{CANARY} for url 'http://faultline-api:8000/extract/rewrite' {_DB_PASSWORD_SHAPED}")

    # (1) per-chunk: the extractor raises → (0, 0, 1, reason)
    def _raise(*_a, **_k):
        raise boom
    monkeypatch.setattr(E, "_document_chunk_edges", _raise)
    monkeypatch.setattr(E.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network")))
    c, s, f, reason = E._process_document_chunk("some text", idx=0, doc_id=1, user_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
                                                backend_url="http://faultline-api:8000", statement_route="rewrite",
                                                source_ref="t")
    assert f == 1 and reason
    assert CANARY not in reason and "hunter2" not in reason and "faultline-api" not in reason, reason
    assert _REF.search(reason), reason

    # (2) the typed chunk refusal keeps its AUTHORED reason code
    ce = E.DocumentChunkExtractionFailed("extractor_status=degraded: ")
    from src.api import errors as ERR
    assert ERR.public_detail(ce, where="t") == "extractor_status=degraded: "

    # (3) the per-document catastrophic write and the brain-unavailable message
    assert CANARY not in ERR.public_detail(boom, where="t", what="document processing failed")
    bu = E.DocumentBrainUnavailable("backend unreachable: " + ERR.public_detail(boom, where="t", what="ValueError"))
    assert "faultline-api" not in str(bu) and "hunter2" not in str(bu) and ERR.is_refusal(bu)


# ── round 3 nits ─────────────────────────────────────────────────────────────────────

def test_provider_detail_masks_our_canaries_and_short_labelled_secrets(caplog):
    """provider_detail redacts the provider sentence BEFORE it travels — and since the
    CQ-hardening contract change (2026-09-17) the sentence travels SERVER-SIDE ONLY: the
    client string is curated, so the masks must appear in the ``faultline.errors`` log record,
    and no secret may appear in either channel. (The redaction ORDER pin — our redactor's
    canaries before brain_fault's provider rules — is unchanged: brain_fault alone chopped a
    flu_ token into fragments and left an 8-char-floor tail, and let `api_key=abc12` through.)"""
    import logging as _logging
    from saas import errors as SE, keys, tokens
    k, _kp, _ = keys.generate_key()
    t, _tp, _ = tokens.generate_user_token()
    exc = _Boom(f"401 for url 'https://x/v1?api_key=abc12' bearer {k} token {t} Incorrect API key provided: sk-live-ZZZZ")
    with caplog.at_level(_logging.WARNING, logger="faultline.errors"):
        out = SE.provider_detail(exc, secrets=["sk-live-ZZZZ"])
    for secret in (k.split("_", 2)[2], t.split("_", 2)[2], "abc12", "ZZZZ"):
        assert secret not in out, (secret, out)
        assert secret not in caplog.text, (secret, caplog.text)
    assert f"[masked {keys.prefix_of(k)}]" in caplog.text, caplog.text
    assert f"[masked {tokens.prefix_of(t)}]" in caplog.text, caplog.text


def test_provider_detail_client_sentence_is_curated_not_the_exception_text(caplog):
    """THE CQ-HARDENING CONTRACT (2026-09-17): a response must never carry the provider's or
    the traceback's own words — the client sentence is the exception CLASS plus an authored
    reason, and the full redacted sentence goes to the server log only."""
    import logging as _logging
    from saas import errors as SE
    exc = _Boom("provider says: Cannot connect to host 10.0.0.7:8080 session abcdef1234567890")
    with caplog.at_level(_logging.WARNING, logger="faultline.errors"):
        out = SE.provider_detail(exc, what="call failed")
    assert "provider says" not in out, out
    assert "10.0.0.7" not in out and "abcdef1234567890" not in out, out
    assert out.startswith("call failed: _Boom: "), out
    assert "provider says" in caplog.text, caplog.text


def test_provider_detail_refusal_keeps_its_authored_sentence():
    """A refusal WE worded is meant for the caller: EgressBlocked keeps its authored sentence
    (with the class name, exactly the shape the detect-cap pins read), unlike an unexpected
    provider failure whose text is withheld."""
    from saas import errors as SE
    from saas.llm_config import EgressBlocked
    exc = EgressBlocked("endpoint URL rejected: private address")
    out = SE.provider_detail(exc, what="call failed")
    assert out == "call failed: EgressBlocked: endpoint URL rejected: private address", out


def test_probe_fault_dict_stays_server_side():
    """The derived ``fault`` dict (provider body sentence + transport exception text) is
    SERVER-side diagnostics: it rides the alarm stream (``brain_health.probe_event``, called
    inside ``_run_test_and_calibrate`` before the response is built), never the test-response
    body. The three routes that used to ``return result`` raw were the last exception-text
    channel (CQ-hardening 2026-09-17): each now projects the response FIELD-BY-FIELD inline —
    the same shape that kept the two recalibrate routes clean to the analyser (a projection
    helper called with the raw dict kept the alert alive through two scan iterations)."""
    from saas import llm_admin as la, tenant_console as tc, user_console as uc
    res = {"ok": False, "model": "m",
           "fault": {"kind": "unknown", "transport_detail": "transport words 10.0.0.9"},
           "detail": "call failed: RuntimeError: provider message withheld"}
    # the projection helper is gone; the routes build their responses from explicit .get
    # reads of the ALLOWED fields — ``fault`` is never among them
    for mod in (la, tc, uc):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "client_probe_result" not in src, mod.__name__
        assert '"ok": result.get("ok")' in src, mod.__name__
    # and the SERVER-side arm keeps the fault (the alarm stream stores it verbatim)
    la_src = pathlib.Path(la.__file__).read_text(encoding="utf-8")
    assert '"fault": _fault' in la_src


def test_stdio_prompts_get_arms_are_generic(monkeypatch):
    """Door parity: the stdio door's prompts/get echoed {e} on both arms (round-2 residual 4).
    Drives run_mcp_server with two requests on a fake stdin (the loop reads stdin.readline in an
    executor and ends on EOF) and captures what _send would write."""
    import asyncio, io, json as _json
    import src.mcp.server as _mcp
    from src.mcp import prompts as P
    name = P.PROMPTS[0]["name"]
    calls = {"n": 0}

    def flaky_prompt(topic=None, **_k):   # 1st call: missing arg TypeError; 2nd: raises canary
        calls["n"] += 1
        if calls["n"] == 1 and topic is None:
            raise TypeError("flaky_prompt() missing 1 required positional argument: 'topic'")
        raise _Boom(CANARY)
    # the loop filters arguments by signature: make `topic` REQUIRED so the first call is a TypeError
    import inspect as _inspect
    sig = _inspect.Signature([_inspect.Parameter("topic", _inspect.Parameter.POSITIONAL_OR_KEYWORD)])
    flaky_prompt.__signature__ = sig
    monkeypatch.setitem(P.PROMPTS[0], "fn", flaky_prompt)
    sent = []
    monkeypatch.setattr(_mcp, "_send", lambda msg: sent.append(msg))
    monkeypatch.setattr(_mcp, "_log", lambda *a, **k: None)
    reqs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ie3", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "prompts/get", "params": {"name": name, "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": name, "arguments": {"topic": "x"}}}]
    monkeypatch.setattr(_mcp.sys, "stdin", io.StringIO("\n".join(_json.dumps(r) for r in reqs) + "\n"))
    asyncio.run(_mcp.run_mcp_server())
    by_id = {m.get("id"): m for m in sent if isinstance(m, dict)}
    assert by_id[1]["error"]["code"] == -32602
    assert "missing required argument(s): topic" in by_id[1]["error"]["message"]
    assert "flaky_prompt" not in by_id[1]["error"]["message"]
    assert by_id[2]["error"]["code"] == -32603
    assert CANARY not in by_id[2]["error"]["message"] and "(ref " in by_id[2]["error"]["message"]


# ── round 4: the Stripe adapter's "secret-free" reason is the seam, driven on a live route ──

def test_admin_payment_config_test_route_renders_no_raw_stripe_exception(saas_on, monkeypatch, caplog):
    """payments_stripe._secret_free_reason used to return str(exc)[:500] with only sk_/pk_/whsec_
    masked and the census allow-listed it by NAME; POST /admin/payment-config/test rendered the
    raw sentence ("Stripe verification failed: <canary ?api-key=sk-proj-… DSN>"). Then it was the
    provider-diagnostic seam with Stripe's words redacted in the body; since the CQ-hardening
    contract change (2026-09-17) the body is CURATED and Stripe's redacted words live in the
    server log only."""
    import logging as _logging
    from saas import admin, payments as _payments
    from saas import payments_stripe as ps
    canary_exc = RuntimeError(f"{CANARY} for url 'https://api.stripe.example/v1?api-key=sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789' postgresql://svc:hunter2-SECRET@db.internal:5432/x")
    prov = object.__new__(ps.StripePaymentProvider)
    prov._mode = "test"; prov._api_key = "sk_test_placeholder"; prov._config = {}
    monkeypatch.setattr(ps.StripePaymentProvider, "is_configured", lambda self: True, raising=False)
    monkeypatch.setattr(ps.StripePaymentProvider, "_bind_key", lambda self: "sk_test_placeholder", raising=False)
    monkeypatch.setattr(ps._stripe.Account, "retrieve", lambda *a, **k: (_ for _ in ()).throw(canary_exc), raising=False)
    monkeypatch.setattr(_payments, "get_payment_provider", lambda: prov)
    monkeypatch.setattr(_payments, "provider_status", lambda: {"provider": "stripe"}, raising=False)
    app = FastAPI(); app.include_router(admin.router)
    with caplog.at_level(_logging.WARNING, logger="faultline.errors"):
        r = TestClient(app).post("/admin/payment-config/test", headers=_op())
    assert r.status_code == 200, r.text
    body = r.text
    # provider-diagnostic shape: the BODY is curated (class + authored reason) — no credential
    # AND no raw provider sentence reaches it; the redacted sentence is server-side
    for leak in ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "sk-proj-", "hunter2", "svc:", CANARY):
        assert leak not in body, (leak, body)
    assert "Stripe verification failed: stripe: RuntimeError" in r.json()["message"]
    assert "[masked]" in caplog.text, caplog.text
    assert "hunter2" not in caplog.text and "svc:" not in caplog.text, caplog.text
