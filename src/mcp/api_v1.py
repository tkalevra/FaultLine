"""FaultLine public developer API — versioned `/v1/` facade (THIRD DOOR).

This module is a THIN FACADE. It re-implements NO memory logic. Every endpoint:

  1. authenticates with the EXISTING bearer dependency (``require_auth``),
  2. resolves the tenant with the EXISTING BOLA-safe seam (``resolve_user_id`` →
     ``bind_tenant``) so a caller can NEVER address another tenant's memory,
  3. runs the EXISTING SaaS gates (agent-mode write freeze, hard usage caps),
  4. calls the EXISTING tool function an MCP/REST call would call
     (``recall_memory_tool`` / ``remember_facts_tool`` / …), and
  5. meters via the EXISTING meter (``metering.bump_for_tool``),

then shapes the tool's return dict into a clean, versioned public JSON envelope.
A developer hitting ``/v1`` gets the byte-for-byte-equivalent validated result an
MCP client gets, because it is the same code path past the transport boundary.

Open-core vs SaaS: the router ships in the open MCP transport (self-hosters get it
like the REST shim). With ``SAAS_MODE`` off, every ``saas_*`` helper passed in is a
no-op, so ``/v1`` behaves byte-for-byte like the open engine. With ``SAAS_MODE`` on,
those same helpers activate account isolation, metering, quotas, and agent-mode.

The router is built via ``build_router(...)`` which is handed the already-defined
helper callables from ``http_server`` — this avoids an import cycle and keeps this
module free of any business logic or transport wiring of its own.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

import src.mcp.premise as _premise   # THE PREMISE — one source, every door
import src.mcp.server as _srv        # the failure verdict — one source, every door
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

API_VERSION = "1.0.0"


# ── Public request models (clean, resource-oriented) ──────────────────────────

class MemoryWriteRequest(BaseModel):
    text: str = Field(
        ...,
        description="The user's own words — a HUMAN being's conversational turn — VERBATIM. "
                    "Never the agent's own working text (briefs, plans, analyses, tool "
                    "output). Do NOT pre-extract, summarize, or restructure into "
                    "facts/triples — FaultLine's engine does all extraction, typing, and "
                    "structuring; it needs the raw words.",
    )
    user_id: str = Field(
        "",
        description="Tenant/seat UUID. Optional when a per-seat (flu_) token is used or the "
                    "X-FaultLine-User-Id / X-OpenWebUI-User-Id header carries it.",
    )


class DocumentWriteRequest(BaseModel):
    text: str = Field(
        ...,
        description="Full document/long-form text, VERBATIM — not summarized. Must be a "
                    "document the USER shared; never the agent's own working text or "
                    "session output.",
    )
    source_ref: str = Field("", description="Optional citation: URL or filename.")
    title: str = Field("", description="Optional document title (used as source_ref if that is unset).")
    user_id: str = Field("", description="Tenant/seat UUID (see MemoryWriteRequest.user_id).")


class RecallRequest(BaseModel):
    query: str = Field(
        ...,
        description="The user's own question/message VERBATIM and in full (a HUMAN asking) — "
                    "do NOT reduce it to a keyword; the engine extracts the topic and "
                    "detects corrections itself. Operational history (commits, builds, logs, "
                    "past work sessions) is not user memory — use the cortex_recall tool.",
    )
    user_id: str = Field("", description="Tenant/seat UUID (see MemoryWriteRequest.user_id).")


class CorrectionRequest(BaseModel):
    text: str = Field(
        ...,
        description="The user's own correction or retraction request, VERBATIM — never the "
                    "agent's working text (e.g. 'actually she lives in Toronto now', "
                    "'forget my old email').",
    )
    user_id: str = Field("", description="Tenant/seat UUID (see MemoryWriteRequest.user_id).")


class ForgetRequest(BaseModel):
    subject: str = Field(
        ...,
        description="WHOSE fact to forget — exactly ONE named target ('me', or a named "
                    "person/thing). There is NO bulk/everything wipe.",
    )
    rel_type: Optional[str] = Field(
        None, description="Optional relation to pin the fact (e.g. has_email, occupation, has_pet).")
    old_value: Optional[str] = Field(
        None, description="Optional value/object to pin exactly one fact.")
    user_id: str = Field("", description="Tenant/seat UUID (see MemoryWriteRequest.user_id).")


class PatientMintRequest(BaseModel):
    external_patient_id: str = Field(
        ...,
        description="The app's OWN opaque patient handle (EMR chart number / internal id). "
                    "NEVER PHI — do NOT send a name, DOB, SSN, or any identifying string. "
                    "FaultLine derives a stable isolated patient seat from it deterministically.",
    )


class LearnRequest(BaseModel):
    text: str = Field(
        ...,
        description="Ontological statements, one per line ('X is a subclass of Y', "
                    "'X is an instance of Y', 'X is a part of Y'), OR an /expand topic command "
                    "('/expand networking online https://…').",
    )
    user_id: str = Field("", description="Tenant/seat UUID (see MemoryWriteRequest.user_id).")


# ── Envelope + status→HTTP mapping (NO business logic — pure shaping) ──────────

# Tool/gate status strings that are NOT hard errors but must map to a non-200 HTTP
# status in the clean API. Everything else is a 200 success envelope.
_STATUS_HTTP = {
    "provisioning": (503, "provisioning"),
    "quota_exceeded": (402, "quota_exceeded"),
    "read_only": (409, "read_only"),
    "ingest_disabled": (409, "read_only"),
    # API-access add-on OFF (migration 160): the account has not enabled the paid /v1 add-on.
    # A clean, NON-billed 403 — never a silent 200, never a meter bump.
    "api_not_enabled": (403, "api_not_enabled"),
    # Account add-on ON but THIS seat is not granted /v1 (migration 161, per-seat grant): a
    # distinct, clean, NON-billed 403 so the caller/admin can tell it apart from the account
    # ceiling being off. Same fail-closed, never-metered contract.
    "seat_api_not_enabled": (403, "seat_api_not_enabled"),
    # Clinical/patient lane (migration 169). Distinct, clean, NON-billed 4xx so a caller/admin
    # can tell the two authority failures apart from the API-addon ones.
    "patient_lane_disabled": (403, "patient_lane_disabled"),   # account ceiling OFF
    "insufficient_scope": (403, "insufficient_scope"),         # key lacks 'provision_patients'
    "capacity_exceeded": (402, "capacity_exceeded"),           # patient capacity block full
}


def _api_error(code: str, message: str, status: int, detail: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    headers = {}
    if status in (429, 503):
        headers["Retry-After"] = "1"
    return JSONResponse(body, status_code=status, headers=headers)


# HTTP status (raised by require_auth / bind_tenant as HTTPException) → error.code.
_HTTP_CODE = {400: "bad_request", 401: "unauthorized", 403: "forbidden",
              404: "not_found", 429: "rate_limited"}


def http_exception_body(status_code: int, detail: Any) -> dict[str, Any]:
    """Shape a raised HTTPException into the consistent /v1 error envelope.

    Used by an http_server exception handler SCOPED to /v1 paths so 401/403/400/429 from
    the shared auth/BOLA seams surface as {"error": {...}} without altering the non-/v1
    routes' default {"detail": ...} behavior.
    """
    code = _HTTP_CODE.get(status_code, "internal" if status_code >= 500 else "error")
    return {"error": {"code": code, "message": str(detail)}}


def _envelope(object_type: str, result: dict[str, Any],
              extra: dict[str, Any] | None = None) -> JSONResponse:
    """Shape a tool result dict into the clean public envelope OR a mapped error.

    Pure: it inspects the tool's own ``status`` (the SAME dict an MCP client receives,
    preserved under ``data``) and lifts the common fields. A recognized non-success
    status maps to its HTTP code; anything else is a 200 success envelope.
    """
    if not isinstance(result, dict):
        # A tool should always return a dict; be defensive rather than 500.
        return JSONResponse({"object": object_type, "data": result})

    status = str(result.get("status") or "").strip().lower()
    mapped = _STATUS_HTTP.get(status)
    if mapped is not None:
        http_status, code = mapped
        return _api_error(
            code,
            str(result.get("message") or result.get("detail") or code.replace("_", " ")),
            http_status,
            detail={k: v for k, v in result.items() if k not in ("status", "message", "detail")}
            or None,
        )

    body: dict[str, Any] = {"object": object_type}
    if extra:
        body.update(extra)
    if status:
        body["status"] = result.get("status")
    # THIS IS A DOOR ONTO THE SAME HANDLERS, so it owes the caller the same verdict.
    # Everything _STATUS_HTTP does not recognise falls through to a 200 success envelope —
    # which silently included every genuine failure the tools can produce: a document ingest
    # whose status is the runtime-computed "failed", a "degraded" write that stored nothing, a
    # cortex "soft_error". A /v1 caller saw 200 and no signal at all.
    #
    # The flag is derived from the same single source the other two doors use, so the three
    # cannot drift: server.status_is_failure.
    #
    # NOTE ON HTTP CODES, deliberately unchanged: `read_only` and `ingest_disabled` map to 409
    # here while the choke point calls them modes rather than errors. That is not a
    # contradiction — an HTTP status answers "can this request proceed", `isError` answers "did
    # the tool do what was asked", and a write refused by a mode legitimately conflicts. What is
    # NOT fixed here is the converse: an unrecognised FAILURE still returns 2xx. Changing that
    # is a breaking change to a versioned public API and is the owner's call; see RESIDUALS.
    # Two ways a result can be a failure, and this door owes the caller both: a status the
    # shared verdict calls a failure, OR a flag the handler set itself. The second matters
    # because some failures carry no status at all — cortex_recall's transport exits return
    # prose and nothing else — so a status-keyed check alone silently drops them here.
    if _srv.status_is_failure(result.get("status")) or result.get("isError") is True:
        body["isError"] = True
    # Lift common ergonomic fields when present (never fabricate).
    for key in ("committed", "staged", "total", "message", "memory", "harvested", "grounded"):
        if key in result:
            body[key] = result[key]
    body["data"] = result
    return JSONResponse(body)


def _v1_docs_public() -> bool:
    """Whether the /v1 spec + interactive docs are served anonymously (audit LOW #10).

    SaaS off / public core → True (unchanged). SAAS_MODE on → False (spec requires auth, the
    interactive UI is removed) unless the operator sets MCP_ENABLE_DOCS truthy. Fail-safe: an
    import fault (public core without the saas package) → True."""
    v = str(os.environ.get("MCP_ENABLE_DOCS") or "").strip().lower()
    if v not in ("", "0", "false", "no", "off"):
        return True  # explicit operator opt-in
    try:
        from saas.config import saas_mode_enabled
        return not saas_mode_enabled()
    except Exception:
        # Open core: no saas.config to consult. An ImportError must not silently
        # re-enable what the operator turned off — honor the raw SAAS_MODE env
        # flag (FOSS runs with it unset: /v1 docs stay public, unchanged).
        if (os.environ.get("SAAS_MODE") or "").strip().lower() not in ("", "0", "false", "no"):
            return False
        return True


def build_router(
    *,
    require_auth: Callable[..., str],
    resolve_user_id: Callable[[Request, str, Optional[str]], str],
    ensure_provisioned: Callable[[str], Awaitable[Any]],  # tri-state gate: truthy=proceed; falsy=wait or terminal
    write_block: Callable[[Optional[str], str, str], Optional[dict]],
    quota_block: Callable[[Optional[str], str, str], Optional[dict]],
    api_addon_block: Callable[[Optional[str], str], Optional[dict]],
    meter: Callable[..., Awaitable[None]],  # (principal, user_id, tool_name, *, api=False)
    tools: Any,
    log: Callable[[str], None],
    # ── clinical/patient lane (migration 169) — all OPTIONAL so open-core / self-host and
    #    existing callers/tests that build the router without them keep working (the patient
    #    routes then fail-closed 404 "lane not available"). ──────────────────────────────────
    patient_provisioning_block: Optional[Callable[[Optional[str], Request], Optional[dict]]] = None,
    account_for: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    patients: Any = None,
    # ── clinical mint ATTRIBUTION (migration 170): resolve the presented provisioning key to
    #    {key_id, sub_admin_id} so the minted patient seat traces to the key + owning doctor. ──
    mint_attribution: Optional[Callable[[Request], Optional[dict]]] = None,
) -> APIRouter:
    """Assemble the `/v1` APIRouter from the already-defined transport helpers.

    All arguments are the EXISTING seams from ``http_server`` / ``server`` — passed in
    so this module never imports them (no cycle) and does its own transport wiring.
    ``tools`` is the ``src.mcp.server`` module (the tool functions live there).
    """
    router = APIRouter(prefix="/v1", tags=["v1"])

    def _validation_error(tool_name: str, arguments: dict) -> Optional[JSONResponse]:
        """Run the same input validation the other two doors run, or None if the input is fine.

        This door ran none at all: `POST /v1/recall {"query": ""}` reached the backend and came
        back HTTP 200 with "I don't have any information about that in your memory" — a wrong
        answer wearing the shape of a right one, which is the exact defect the REST door was
        fixed for. Same validator, same 400, same `{"error": ...}` body vocabulary.
        """
        err = _srv._validate_tool_input(tool_name, arguments)
        if err is None:
            return None
        log(f"V1 {tool_name} rejected: {err.get('error')}")
        return JSONResponse(err, status_code=400)

    async def _run_write(
        request: Request,
        principal: str,
        body_user_id: str,
        tool_name: str,
        call: Callable[[str], Awaitable[dict]],
        object_type: str,
        validate: Optional[dict] = None,
    ) -> JSONResponse:
        """Shared write path: the EXACT gate sequence the REST shim runs, then the tool."""
        user_id = resolve_user_id(request, body_user_id, principal)  # BOLA-safe (raises HTTPException)
        if validate is not None:
            invalid = _validation_error(tool_name, {**validate, "user_id": user_id})
            if invalid is not None:
                return invalid
        # API-access add-on gate (migration 160): AFTER auth+BOLA, BEFORE the tool + meter.
        # Fail-closed 403 when the account has not enabled the paid /v1 add-on — never billed.
        addon = api_addon_block(principal, user_id)
        if addon is not None:
            log(f"V1 {tool_name} API-ADDON-OFF user_id={user_id[:8]}...")
            return _envelope(object_type, addon)
        blocked = write_block(principal, user_id, tool_name)
        if blocked is not None:
            log(f"V1 {tool_name} BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
            return _envelope(object_type, blocked)
        quota = quota_block(principal, user_id, tool_name)
        if quota is not None:
            log(f"V1 {tool_name} QUOTA {quota.get('metric')} user_id={user_id[:8]}...")
            return _envelope(object_type, quota)
        _gate = await ensure_provisioned(user_id)
        if not _gate:  # honest wait OR terminal provisioning failure — the ONE envelope renderer
            from src.mcp import server as _gate_srv
            return _envelope(object_type, _gate_srv.provisioning_gate_envelope(_gate))
        result = await call(user_id)
        # The same choke point the other two doors run — including the third arm, "did this
        # operation actually do anything?", which is the forget/retract zero-count hole. The
        # /v1 envelope lifts isError itself, but only from a flag set HERE; without this the
        # /v1 door would silently keep answering a retracted=0 tombstone as a success.
        result = _srv.stamp_is_error(result, tool_name)
        # api=True → also bump the transport-scoped api_calls meter (this is the /v1 door).
        await meter(principal, user_id, tool_name, api=True)
        return _envelope(object_type, result)

    # ── health / spec / docs ──────────────────────────────────────────────────
    # /health stays open. Under SAAS_MODE the spec + interactive docs are LOCKED DOWN
    # (audit LOW #10): the /v1 spec maps the surface incl. the /v1/patients PHI lane, so it
    # is no longer served anonymously in prod. /v1/openapi.json then requires the same bearer
    # as the API itself (an SDK client already holds a key), and the interactive /v1/docs UI
    # is removed (404). SAAS off / MCP_ENABLE_DOCS=1 → both stay open (unchanged).
    _docs_open = _v1_docs_public()
    _spec_deps = [] if _docs_open else [Depends(require_auth)]

    @router.get("/health")
    async def v1_health() -> JSONResponse:
        return JSONResponse({"status": "ok", "api": "faultline", "version": API_VERSION})

    @router.get("/openapi.json", include_in_schema=False, dependencies=_spec_deps)
    async def v1_openapi() -> JSONResponse:
        return JSONResponse(_build_v1_openapi())

    if _docs_open:
        @router.get("/docs", include_in_schema=False)
        async def v1_docs() -> HTMLResponse:
            return HTMLResponse(_V1_DOCS_HTML)

    # ── memories: remember ────────────────────────────────────────────────────

    @router.post("/memories", summary="Remember facts from a message")
    async def v1_remember(
        request: Request, body: MemoryWriteRequest, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        return await _run_write(
            request, principal, body.user_id, "remember_facts",
            lambda uid: tools.remember_facts_tool(text=body.text, user_id=uid),
            "memory.write",
            validate={"text": body.text},
        )

    # ── documents: bulk / long-form ingest ────────────────────────────────────

    @router.post("/documents", summary="Ingest a document / long-form text")
    async def v1_document(
        request: Request, body: DocumentWriteRequest, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        return await _run_write(
            request, principal, body.user_id, "ingest_document",
            lambda uid: tools.ingest_document_tool(
                text=body.text, user_id=uid, source_ref=body.source_ref, title=body.title),
            "document.write",
            validate={"text": body.text},
        )

    # ── recall (POST + GET alias) ─────────────────────────────────────────────

    async def _recall(request: Request, query: str, body_user_id: str, principal: str) -> JSONResponse:
        user_id = resolve_user_id(request, body_user_id, principal)  # BOLA-safe
        invalid = _validation_error("recall_memory", {"query": query, "user_id": user_id})
        if invalid is not None:
            return invalid
        # Recall on the /v1 API is a billable transaction too — gate the whole door (DESIGN §2).
        addon = api_addon_block(principal, user_id)
        if addon is not None:
            log(f"V1 recall_memory API-ADDON-OFF user_id={user_id[:8]}...")
            return _envelope("recall", addon)
        _gate = await ensure_provisioned(user_id)
        if not _gate:
            from src.mcp import server as _gate_srv
            _env = _gate_srv.provisioning_gate_envelope(_gate)
            # ONE way on /v1 (round 9): a TERMINAL provisioning failure is a 409-class FACT about
            # the seat (not a 503 "retry later"); the ids ride as FIELDS in `error.detail`, never
            # only in prose, so an API client can act on the correlation id.
            if _env.get("terminal"):
                return _api_error(_env["status"], _env["message"], 409, detail={
                    "terminal": True,
                    "provisioning_status": _env.get("provisioning_status"),
                    "correlation_id": _env.get("correlation_id"),
                    "error_message": _env.get("error_message"),
                })
            return _api_error(_env["status"], _env["message"], 503)
        result = await tools.recall_memory_tool(query=query, user_id=user_id)
        await meter(principal, user_id, "recall_memory", api=True)  # /v1 door → api_calls too
        # THROUGH THE SAME SEAM. This route used to build its own body and return it raw, so a
        # failing recall came back from /v1 as HTTP 200 with no isError and no status at all —
        # the one mount of three that silently dropped the verdict.
        return _envelope("recall", result if isinstance(result, dict) else {},
                         extra={"query": query})

    @router.post("/recall", summary="Recall grounded memory as prose")
    async def v1_recall(
        request: Request, body: RecallRequest, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        return await _recall(request, body.query, body.user_id, principal)

    @router.get("/memories", summary="Recall (convenience alias of POST /v1/recall)")
    async def v1_recall_get(
        request: Request, q: str = "", user_id: str = "", principal: str = Depends(require_auth)
    ) -> JSONResponse:
        if not q.strip():
            return _api_error("bad_request", "query parameter 'q' is required", 400)
        return await _recall(request, q, user_id, principal)

    # ── corrections / retractions ─────────────────────────────────────────────

    @router.post("/corrections", summary="Correct or retract a fact (natural language)")
    async def v1_correction(
        request: Request, body: CorrectionRequest, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        return await _run_write(
            request, principal, body.user_id, "retract_fact",
            lambda uid: tools.retract_fact_tool(text=body.text, user_id=uid),
            "correction",
            validate={"text": body.text},
        )

    # ── forget one named fact (bounded) ───────────────────────────────────────

    @router.delete("/memories", summary="Forget ONE specific named fact (bounded tombstone)")
    async def v1_forget(
        request: Request, body: ForgetRequest, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        if not body.subject.strip():
            return _api_error("bad_request",
                              "subject is required — a forget must name exactly one target", 400)
        return await _run_write(
            request, principal, body.user_id, "forget_fact",
            lambda uid: tools.forget_fact_tool(
                user_id=uid, subject=body.subject, rel_type=body.rel_type, old_value=body.old_value),
            "forget",
            validate={"subject": body.subject, "rel_type": body.rel_type,
                      "old_value": body.old_value},
        )

    # ── learn / expand ────────────────────────────────────────────────────────

    @router.post("/learn", summary="Learn ontology or /expand a topic")
    async def v1_learn(
        request: Request, body: LearnRequest, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        return await _run_write(
            request, principal, body.user_id, "learn_facts",
            lambda uid: tools.learn_facts_tool(text=body.text, user_id=uid),
            "learn",
            validate={"text": body.text},
        )

    # ── clinical/patient lane (migration 169) — mint/status/deprovision an ISOLATED ──────────
    #    per-patient schema. The mint/deprovision verbs are gated by
    #    ``patient_provisioning_block`` (account ceiling ON + key carries 'provision_patients' +
    #    /v1 add-on ceiling ON) BEFORE any write. Memory ops on a patient are UNCHANGED — the
    #    app addresses them via ``X-FaultLine-User-Id: <patient_id>`` exactly like any seat.

    def _lane_available() -> bool:
        return not (patients is None or patient_provisioning_block is None or account_for is None)

    @router.post("/patients", summary="Provision an isolated patient seat (clinical lane)")
    async def v1_patient_mint(
        request: Request, body: PatientMintRequest, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        if not _lane_available():
            return _api_error("not_found", "the clinical patient lane is not available", 404)
        block = patient_provisioning_block(principal, request)  # type: ignore[misc]
        if block is not None:
            log("V1 patients MINT DENIED (lane gate)")
            return _envelope("patient", block)
        account_id = account_for(principal)  # type: ignore[misc]
        if not account_id:
            return _api_error("forbidden", "a provisioning account context is required", 403)
        ext = (body.external_patient_id or "").strip()
        if not ext:
            return _api_error("bad_request",
                              "external_patient_id is required (opaque handle, NEVER PHI)", 400)
        # Attribution: resolve the presented provisioning key → (key_id, owning doctor) so the
        # patient seat traces back to who minted it. Best-effort — a resolver miss mints without
        # attribution rather than failing an authorized mint.
        attr = {}
        if mint_attribution is not None:
            try:
                attr = mint_attribution(request) or {}
            except Exception:
                attr = {}
        res = patients.mint_patient(
            account_id, ext,
            provisioned_by_key_id=attr.get("key_id"),
            provisioned_by_sub_admin=attr.get("sub_admin_id"))
        if not res.get("ok"):
            sc = int(res.get("status_code", 500))
            code = {402: "capacity_exceeded", 409: "conflict", 404: "not_found",
                    400: "bad_request"}.get(sc, "internal" if sc >= 500 else "error")
            return _api_error(code, res.get("reason", "could not provision patient"), sc,
                              detail={"patient_id": res.get("patient_id")})
        pid = res["patient_id"]
        # Kick the async schema job in the background (~1 min) so the mint returns immediately;
        # the app polls GET /v1/patients/{id} for readiness (intake-time provisioning).
        try:
            from src.mcp import server as _bg_srv
            _bg_srv._background(ensure_provisioned(pid))  # round 16: outside the request's turn wall
        except Exception:
            pass
        log(f"V1 patients MINT ok patient={pid[:8]}...")
        return JSONResponse(
            {"object": "patient", "patient_id": pid, "external_patient_id": ext,
             "status": res.get("status", "provisioning")},
            status_code=201,
        )

    @router.get("/patients/{patient_id}", summary="Patient provisioning status + seat state")
    async def v1_patient_status(
        request: Request, patient_id: str, principal: str = Depends(require_auth)
    ) -> JSONResponse:
        # Reading status is looser than minting (not a provisioning act): any key authorized for
        # the account may read, and BOLA is enforced by get_patient scoping to (account, patient).
        if not _lane_available():
            return _api_error("not_found", "the clinical patient lane is not available", 404)
        account_id = account_for(principal)  # type: ignore[misc]
        if not account_id:
            return _api_error("forbidden", "an account context is required", 403)
        info = patients.get_patient(account_id, patient_id)
        if info is None:
            return _api_error("not_found", "no such patient for this account", 404)
        return JSONResponse(info)

    @router.delete("/patients/{patient_id}", summary="Off-board a patient (revoke / purge)")
    async def v1_patient_deprovision(
        request: Request, patient_id: str, purge: bool = False,
        principal: str = Depends(require_auth)
    ) -> JSONResponse:
        if not _lane_available():
            return _api_error("not_found", "the clinical patient lane is not available", 404)
        block = patient_provisioning_block(principal, request)  # type: ignore[misc]
        if block is not None:
            log("V1 patients DEPROVISION DENIED (lane gate)")
            return _envelope("patient", block)
        account_id = account_for(principal)  # type: ignore[misc]
        if not account_id:
            return _api_error("forbidden", "a provisioning account context is required", 403)
        res = patients.deprovision_patient(account_id, patient_id, purge=purge)
        if not res.get("ok"):
            return _api_error("not_found", res.get("reason", "patient not found"),
                              int(res.get("status_code", 404)))
        log(f"V1 patients DEPROVISION ok patient={patient_id[:8]}... purge={purge}")
        return JSONResponse(
            {"object": "patient", "patient_id": patient_id,
             "status": res.get("status", "revoked"), "purged": bool(res.get("purged", False))})

    return router


# ── Hand-built /v1 OpenAPI 3.0.3 spec (the contract of record for /v1) ─────────

_V1_OPENAPI_CACHE: dict | None = None


def _build_v1_openapi() -> dict:
    global _V1_OPENAPI_CACHE
    if _V1_OPENAPI_CACHE is not None:
        return _V1_OPENAPI_CACHE

    def _json_body(props: dict, required: list[str]) -> dict:
        return {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "properties": props, "required": required}}}}

    ok = {"200": {"description": "Result",
                  "content": {"application/json": {"schema": {"type": "object"}}}}}
    sec = [{"HTTPBearer": []}]
    paths = {
        "/v1/memories": {
            "post": {"operationId": "remember", "summary": "Remember facts from a message",
                     "security": sec,
                     "requestBody": _json_body(
                         {"text": {"type": "string"}, "user_id": {"type": "string"}}, ["text"]),
                     "responses": ok},
            "get": {"operationId": "recall_get", "summary": "Recall (alias)", "security": sec,
                    "parameters": [
                        {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "user_id", "in": "query", "required": False, "schema": {"type": "string"}}],
                    "responses": ok},
            "delete": {"operationId": "forget", "summary": "Forget ONE named fact", "security": sec,
                       "requestBody": _json_body(
                           {"subject": {"type": "string"}, "rel_type": {"type": "string"},
                            "old_value": {"type": "string"}, "user_id": {"type": "string"}}, ["subject"]),
                       "responses": ok},
        },
        "/v1/documents": {"post": {
            "operationId": "ingest_document", "summary": "Ingest a document", "security": sec,
            "requestBody": _json_body(
                {"text": {"type": "string"}, "source_ref": {"type": "string"},
                 "title": {"type": "string"}, "user_id": {"type": "string"}}, ["text"]),
            "responses": ok}},
        "/v1/recall": {"post": {
            "operationId": "recall", "summary": "Recall grounded memory as prose", "security": sec,
            "requestBody": _json_body(
                {"query": {"type": "string"}, "user_id": {"type": "string"}}, ["query"]),
            "responses": ok}},
        "/v1/corrections": {"post": {
            "operationId": "correct", "summary": "Correct or retract a fact", "security": sec,
            "requestBody": _json_body(
                {"text": {"type": "string"}, "user_id": {"type": "string"}}, ["text"]),
            "responses": ok}},
        "/v1/learn": {"post": {
            "operationId": "learn", "summary": "Learn ontology or /expand a topic", "security": sec,
            "requestBody": _json_body(
                {"text": {"type": "string"}, "user_id": {"type": "string"}}, ["text"]),
            "responses": ok}},
        "/v1/health": {"get": {"operationId": "health", "summary": "Liveness + version",
                               "responses": ok}},
        # Clinical lane — elevated app key (flk_ carrying the 'provision_patients' scope) mints an
        # isolated patient seat; the app then addresses that patient's memory through the ordinary
        # /v1 surface with header X-FaultLine-User-Id: <patient_id>. Gated fail-closed.
        "/v1/patients": {"post": {
            "operationId": "provision_patient",
            "summary": "Provision an isolated patient seat (clinical lane)",
            "description": "Requires the /v1 API add-on + patient lane enabled + a key scoped "
                           "'provision_patients'. Idempotent on external_patient_id.",
            "security": sec,
            "requestBody": _json_body({"external_patient_id": {"type": "string"}},
                                      ["external_patient_id"]),
            "responses": ok}},
        "/v1/patients/{patient_id}": {
            "get": {"operationId": "patient_status",
                    "summary": "Patient provisioning status + seat state", "security": sec,
                    "parameters": [{"name": "patient_id", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                    "responses": ok},
            "delete": {"operationId": "deprovision_patient",
                       "summary": "Off-board a patient (revoke / optional purge)", "security": sec,
                       "parameters": [
                           {"name": "patient_id", "in": "path", "required": True,
                            "schema": {"type": "string"}},
                           {"name": "purge", "in": "query", "required": False,
                            "schema": {"type": "boolean"}}],
                       "responses": ok}},
    }
    _V1_OPENAPI_CACHE = {
        "openapi": "3.0.3",
        "info": {
            "title": "FaultLine API",
            "version": API_VERSION,
            # /v1 has TWO audiences: a human integrator reading the docs, and an AGENT consuming
            # this spec. The old one-liner served the human and said nothing to the agent — so an
            # agent arriving through this door learned a different (emptier) FaultLine than one
            # arriving over MCP. Human blurb first, then the same premise every other door sends.
            "description": _premise.V1_DESCRIPTION,
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {"securitySchemes": {"HTTPBearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"HTTPBearer": []}],
    }
    return _V1_OPENAPI_CACHE


_V1_DOCS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>FaultLine API v1</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      max-width:820px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fafafa}
 h1{font-size:1.6rem} code,pre{font-family:"JetBrains Mono",ui-monospace,monospace}
 pre{background:#111;color:#eee;padding:1rem;border-radius:8px;overflow-x:auto}
 table{border-collapse:collapse;width:100%;margin:1rem 0}
 td,th{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.92rem}
 .m{font-weight:700;color:#0a6}
</style></head><body>
<h1>FaultLine API — v1</h1>
<p>Per-tenant, write-validated knowledge-graph memory. Authenticate with
<code>Authorization: Bearer &lt;token&gt;</code>. Machine spec:
<a href="/v1/openapi.json">/v1/openapi.json</a>.</p>
<table>
<tr><th>Method</th><th>Path</th><th>Purpose</th></tr>
<tr><td class="m">POST</td><td>/v1/memories</td><td>Remember facts from a message</td></tr>
<tr><td class="m">POST</td><td>/v1/documents</td><td>Ingest a document / long-form text</td></tr>
<tr><td class="m">POST</td><td>/v1/recall</td><td>Recall grounded memory as prose</td></tr>
<tr><td class="m">GET</td><td>/v1/memories?q=</td><td>Recall (convenience alias)</td></tr>
<tr><td class="m">POST</td><td>/v1/corrections</td><td>Correct or retract a fact</td></tr>
<tr><td class="m">DELETE</td><td>/v1/memories</td><td>Forget ONE named fact (bounded)</td></tr>
<tr><td class="m">POST</td><td>/v1/learn</td><td>Learn ontology or /expand a topic</td></tr>
<tr><td class="m">GET</td><td>/v1/health</td><td>Liveness + version (no auth)</td></tr>
</table>
<h2>Quickstart</h2>
<pre>curl -X POST https://your-host/v1/memories \\
  -H "Authorization: Bearer $FAULTLINE_API_KEY" \\
  -H "X-FaultLine-User-Id: $SEAT_UUID" \\
  -H "Content-Type: application/json" \\
  -d '{"text":"My sister Diane is a nurse in Ottawa."}'

curl -X POST https://your-host/v1/recall \\
  -H "Authorization: Bearer $FAULTLINE_API_KEY" \\
  -H "X-FaultLine-User-Id: $SEAT_UUID" \\
  -H "Content-Type: application/json" \\
  -d '{"query":"what does my sister do?"}'</pre>
<p>Tenant is always server-derived from your key/seat — you can never address another
tenant's memory.</p>
</body></html>
"""
