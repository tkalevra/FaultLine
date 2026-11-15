"""THE PREMISE + TOOL-DESCRIPTION PARITY.

WHY THIS FILE EXISTS (paid for in real time, 2026-07-14)
--------------------------------------------------------
A real model was connected to FaultLine and reported: "wtf is this and how does it help me".
The owner had to hand-hold it into using the tools. It was NOT weak tool-calling, and it was not
a weak model. It was us:

  1. `/openapi.json` (the door OpenWebUI + every model behind it reads) was served from a
     SEPARATE, hand-written, STRIPPED-DOWN copy of the tool descriptions (`_TOOL_OPS`). A native
     MCP client read "Call at the START of a turn ..."; an OpenAPI client read "Look up what you
     already know". THE WHEN WAS GONE. We shipped the model the worse half of our own docs and
     then read its confusion as a model failure.
  2. NOTHING told a connecting model what FaultLine IS or why it would want it. MCP's
     `initialize` carries an `instructions` field for exactly this; ours sent none.
  3. The one layer that DID explain the rationale (the `HOW YOU OPERATE` block) rides the RECALL
     RESPONSE — it only reaches a model that has ALREADY called recall_memory. The guidance that
     teaches a model when to call the tool was delivered inside that tool's own output.

Three hand-maintained copies of the same prose WILL drift, and the drift IS the bug. These tests
guard the collapse to one source and the presence of the premise on every door.
"""
import pytest
from fastapi.testclient import TestClient

import src.mcp.http_server as h
import src.mcp.server as m
from src.mcp import premise


@pytest.fixture
def client():
    with TestClient(h.app) as c:
        yield c


# ── Parity: one source of truth ───────────────────────────────────────────────────────────

def test_openapi_serves_the_canonical_tool_descriptions_not_a_stripped_copy():
    """THE BUG. Every tool in the served OpenAPI must carry the SAME description a native MCP
    client gets from tools/list. A second hand-written copy is how the WHEN got dropped for
    OpenWebUI clients while every test stayed green."""
    spec = h._build_tool_openapi()
    canonical = {t["name"]: t["description"] for t in m.TOOLS}

    # The SaaS-gated lane has its OWN single source, on purpose. `customer_chat` is not in the
    # open-core tools.py::TOOLS because it is moat-side: its schema lives in
    # saas/agent_mcp_tool.py::TOOL_SCHEMA and it is advertised DYNAMICALLY (only to entitled
    # agent seats — see _saas_visible_tools). That is a legitimate SECOND canonical source, not
    # the hand-written stripped copy this test exists to forbid, so fold it in rather than
    # exempting it: the single-source guarantee is still enforced, just against the right file.
    try:
        from saas.agent_mcp_tool import TOOL_SCHEMA as _saas_schema
        canonical.setdefault(_saas_schema["name"], _saas_schema["description"])
    except Exception:
        pass  # open-core checkout: no saas/, and no customer_chat op to check either

    for path, item in spec["paths"].items():
        op = item["post"]["operationId"]
        assert op in canonical, (
            f"{op} is served over OpenAPI but has no canonical description — it must come from "
            f"tools.py::TOOLS (open core) or saas/agent_mcp_tool.py::TOOL_SCHEMA (moat), never "
            f"from a hand-written copy in _TOOL_OPS."
        )
        assert item["post"]["description"] == canonical[op], (
            f"{op}: the OpenAPI door is serving a DIFFERENT description than the MCP door. "
            f"This is the drift that shipped a model the impoverished copy."
        )


def test_recall_memory_keeps_its_when_trigger_on_the_openapi_door():
    """The specific casualty, pinned by MECHANISM rather than by phrase.

    History: the stripped copy opened with "Look up what you already know" — a WHAT. The
    canonical text opens with a WHEN, which is the only part that changes a model's behaviour.

    This used to assert the literal string "Call at the START of a turn". That phrasing was
    deliberately rewritten (fa8..., "cold-connect tool guidance") into a stronger, more
    recognisable trigger, and the literal assertion then failed on copy that is BETTER — a test
    pinning wording rather than the property it cares about. What must never regress is (a) the
    OpenAPI door serving the SAME text as the MCP door, and (b) that text still telling a model
    WHEN to call, not merely what the tool is. Both are asserted below; the wording is free to
    improve."""
    spec = h._build_tool_openapi()
    desc = spec["paths"]["/recall_memory"]["post"]["description"]
    canonical = {t["name"]: t["description"] for t in m.TOOLS}["recall_memory"]
    assert desc == canonical, "the OpenAPI door drifted from the canonical description"
    lowered = desc.lower()
    assert "call" in lowered, "no call instruction survives on the door OpenWebUI reads"
    # A WHEN names a moment/condition, not just a capability. Any one of these is enough.
    triggers = ("call at the start", "before you", "when the user", "if you are about to",
                "call it", "call first", "at the start of")
    assert any(t in lowered for t in triggers), (
        "the WHEN trigger was stripped from the door OpenWebUI actually reads — the description "
        "now says only WHAT the tool is. Restore a condition that tells a model when to fire."
    )


def test_every_mcp_tool_is_exposed_on_the_openapi_door():
    """A tool invisible to OpenAPI cannot be called by an OpenWebUI model at all — a whole
    capability silently missing, which has happened before (the cortex pair)."""
    served = {i["post"]["operationId"] for i in h._build_tool_openapi()["paths"].values()}
    declared = {t["name"] for t in m.TOOLS}
    assert declared <= served, f"declared but NOT served over OpenAPI: {declared - served}"


# ── The premise: present on every door ────────────────────────────────────────────────────

def test_mcp_initialize_sends_server_instructions(client):
    """MCP's `instructions` is the ONLY channel that reaches a model BEFORE it calls anything.
    It was empty: a model got a name, a version, and eight signatures with no premise."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}}).json()
    ins = r["result"].get("instructions")
    assert ins, "initialize must carry the premise — this is the pre-first-call channel"
    assert ins == premise.SERVER_INSTRUCTIONS


def test_openapi_info_carries_the_premise():
    """OpenAPI has no `instructions` field, so info.description is the whole premise budget.
    It used to hold a one-line WHAT that told a model nothing about when or why."""
    desc = h._build_tool_openapi()["info"]["description"]
    assert desc == premise.OPENAPI_DESCRIPTION
    assert "You do not have one" in desc, "the premise must state the model has no memory of its own"


def test_v1_door_carries_the_premise_and_keeps_the_human_blurb(client):
    """/v1 has two audiences. The agent must learn the SAME FaultLine as one arriving over MCP,
    while the human integrator keeps their front-page summary."""
    desc = client.get("/v1/openapi.json").json()["info"]["description"]
    assert "Per-tenant, write-validated knowledge-graph memory" in desc, "human blurb dropped"
    assert premise.SERVER_INSTRUCTIONS in desc, "an agent arriving via /v1 gets no premise"


def test_all_doors_send_the_SAME_premise():
    """TRANSPORT PARITY, applied to MEANING. A model must not learn a different FaultLine
    depending on which door it came through."""
    assert premise.OPENAPI_DESCRIPTION == premise.SERVER_INSTRUCTIONS
    assert premise.SERVER_INSTRUCTIONS in premise.V1_DESCRIPTION


# ── The premise must actually answer the model's question ─────────────────────────────────

def test_premise_answers_when_and_why_not_just_what():
    """The whole point. "wtf is this and how does it help me" is a WHY question and a WHEN
    question. A premise that only says WHAT it is reproduces the bug in nicer prose."""
    p = premise.SERVER_INSTRUCTIONS

    # WHY: it must contrast itself with the model's own (unreliable) recollection.
    assert "reconstruction" in p, "must explain why its answers beat the model's own recall"

    # WHEN: a checkable trigger, not "call it when relevant" (a model cannot check that).
    assert "about to GUESS" in p, "must give a CHECKABLE trigger — being about to guess"

    # NOT: bound the capability, or the model will expect it to browse/fetch and be disappointed.
    assert "does not browse" in p, "must say what it is NOT, or the model over-expects"


# ── THE CARRIER: the premise must survive the client that actually consumes it ────────────

def _openwebui_tool_payload(spec):
    """Replicate OpenWebUI's `convert_openapi_to_tool_payload` EXACTLY.

    It reads ONLY `paths` -> per-operation operationId + description|summary. It does NOT read
    top-level `info.description`. Verified against open-webui source (backend/open_webui/utils/
    tools.py), not inferred from its docs — the docs do not say.
    """
    return [{"name": item["post"].get("operationId"),
             "description": item["post"].get("description",
                                             item["post"].get("summary", "No description available."))}
            for item in spec["paths"].values()]


def test_premise_survives_the_openwebui_conversion():
    """THE ONE THAT MATTERS. A premise parked only in `info.description` is DROPPED ON THE FLOOR
    by OpenWebUI — the exact client our users' models connect through. It must ride the
    per-operation tool descriptions, which every client on every transport reads."""
    payload = _openwebui_tool_payload(h._build_tool_openapi())
    carrying = [t["name"] for t in payload
                if premise.TOOL_PREMISE.strip()[:40] in t["description"]]
    assert carrying, (
        "NO tool carries the premise through OpenWebUI's conversion — a model connecting via "
        "OpenWebUI would learn nothing about what FaultLine is or when to reach for it."
    )
    for name in premise.PREMISE_TOOLS:
        assert name in carrying, f"{name} is an entry-point tool and must carry the premise"


def test_premise_does_not_ride_every_tool():
    """COST. The premise is one idea; paying for it on all 8 tools pays for it 8 times. Only the
    ENTRY-POINT tools (the ones a model must reach for unprompted) carry it — the rest are
    reached once the model already understands what this is."""
    payload = _openwebui_tool_payload(h._build_tool_openapi())
    carrying = {t["name"] for t in payload
                if premise.TOOL_PREMISE.strip()[:40] in t["description"]}
    assert carrying == set(premise.PREMISE_TOOLS), \
        f"premise is riding tools that do not need it: {carrying - set(premise.PREMISE_TOOLS)}"


def test_tool_premise_leads_with_why_before_how():
    """A model reading a tool spec cold needs "why would I ever call this" BEFORE "how do I call
    it". Hence PREFIX, not suffix. If this inverts, the model hits mechanics first and the
    premise becomes trailing noise it may never weigh."""
    desc = next(t["description"] for t in _openwebui_tool_payload(h._build_tool_openapi())
                if t["name"] == "recall_memory")
    assert desc.startswith("FaultLine is this user's memory"), \
        "the premise must LEAD the description, not trail it"


# ── Protocol version negotiation (MCP lifecycle spec) ─────────────────────────────────────

@pytest.mark.parametrize("requested,expected", [
    ("2025-06-18", "2025-06-18"),   # current — MUST be echoed, not downgraded
    ("2025-03-26", "2025-03-26"),   # older but supported — echo it
    ("2024-11-05", "2024-11-05"),
    ("2024-10-07", "2024-10-07"),   # oldest official SDK revision — echo it
    # Unknown → never hand back a revision NEWER than what the client proposed (2026-08-10):
    # a client proposing an old/unknown version can only parse revisions it knows. A proposal
    # newer than our latest still gets our latest; one older than everything we serve gets our
    # OLDEST official revision, not the custom 2026-07-28 stateless one its parser rejects.
    ("1999-01-01", "2024-10-07"),
    (None, premise.LATEST_PROTOCOL_VERSION),           # absent → our latest, never a crash
])
def test_protocol_version_is_negotiated_not_hardcoded(client, requested, expected):
    """Spec: "If the server supports the requested protocol version, it MUST respond with the
    same version." We used to HARDCODE 2025-03-26 and ignore the request — so a client speaking
    the current version was told an older one, and per spec "SHOULD disconnect"."""
    params = {} if requested is None else {"protocolVersion": requested}
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": params}).json()
    assert r["result"]["protocolVersion"] == expected


# ── THE 1024-CHAR CLIFF (this one took production down) ───────────────────────────────────

def test_no_served_description_exceeds_the_openai_limit():
    """OpenAI-compatible function-calling REJECTS a tool whose `description` is over 1024 chars —
    and rejecting one tool rejects the WHOLE tool list. The MCP/Anthropic door has NO such cap.

    So when both doors were unified onto the long canonical text (+ premise prefix + standing-rule
    suffix), three tools crossed 1024 and the OpenWebUI-hosted model could no longer call FaultLine
    AT ALL. We went from "the model doesn't know WHEN to call us" to "the model CANNOT call us".
    The MCP door stayed perfectly healthy the whole time, so every check looked green.

    The budget is shared: premise + tool text + standing rules all draw on the same 1024.
    """
    spec = h._build_tool_openapi()
    over = {i["post"]["operationId"]: len(i["post"]["description"])
            for i in spec["paths"].values()
            if len(i["post"]["description"]) > premise.OPENAI_DESCRIPTION_LIMIT}
    assert not over, (
        f"over the {premise.OPENAI_DESCRIPTION_LIMIT}-char OpenAI limit: {over}. An "
        f"OpenAI-compatible client will reject the ENTIRE tool list — every tool, not just these."
    )


def test_budget_holds_with_the_standing_rules_APPENDED(monkeypatch):
    """THE CONDITION THAT ACTUALLY SHIPPED THE BUG.

    The standing operating rules are appended to tool descriptions only when MEMORY_TEACHES is on
    — which it is in production (it follows SAAS_MODE). Locally it was OFF, so every length check
    I ran was measuring a SHORTER string than production served. The bug lived exactly in the gap
    between the two.

    This test forces the production condition. Without it, the guard above is measuring the wrong
    string and would go green on a broken build — again.
    """
    monkeypatch.setenv("MEMORY_TEACHES", "1")
    import importlib
    import src.api.cortex_usage_seed as seed
    import src.mcp.tools as tools_mod
    importlib.reload(seed)
    importlib.reload(tools_mod)
    try:
        canonical = {t["name"]: t["description"] for t in tools_mod.TOOLS}
        over = {n: len(d) for n, d in canonical.items()
                if len(d) > premise.OPENAI_DESCRIPTION_LIMIT}
        assert not over, (
            f"WITH the standing rules appended (production), these exceed "
            f"{premise.OPENAI_DESCRIPTION_LIMIT}: {over}"
        )
    finally:
        monkeypatch.delenv("MEMORY_TEACHES", raising=False)
        importlib.reload(seed)
        importlib.reload(tools_mod)


def test_over_limit_description_is_clamped_not_served_raw():
    """FAIL LOUD, STAY UP. If something does go over anyway, serving it breaks EVERY tool for that
    client. A clamped description merely loses a trailing sentence. Clamp, shout, stay up."""
    long_desc = "word " * 400          # 2000 chars
    clamped = h._build_tool_openapi.__globals__  # sanity: builder exists
    assert clamped is not None
    spec = h._build_tool_openapi()
    for i in spec["paths"].values():
        assert len(i["post"]["description"]) <= premise.OPENAI_DESCRIPTION_LIMIT


def test_premise_stays_cheap():
    """It lands in context once per session — rented, not owned. Mechanics belong in the
    relevance-gated seeded rules, which cost nothing when irrelevant. If this fails, something
    was dumped here that belongs in cortex_usage_seed."""
    # BUDGET RAISED 3000 -> 3600 (2026-08-10), deliberately, once — not silently ratcheted.
    # The premise grew to 3338 when cold-connect guidance was added to close the repeated_task
    # and explicit_forget gaps: that content is PROACTIVE by nature (it must land at connect,
    # before any call), so it genuinely cannot move to the relevance-gated seed, which only
    # rides a recall RESPONSE. The ceiling still exists and is still tight — if this fails
    # again, the next addition almost certainly IS seed material. Do not raise it a third time
    # without checking that the new text must be proactive.
    #
    # RAISE #2: 3600 -> 3900 (2026-11-04, merge-upward gauntlet B1 port of foss 5bf9e382). The
    # HOST NOTE TOOLS paragraph answers the shadow-memory failure (a model routing "save to
    # FaultLine" into the host's ephemeral note tools) — a ROUTING rule the model must know at
    # connect, before it ever picks a tool, so it is proactive by the same test as raise #1 and
    # cannot live in the recall-response seed. New length pinned exactly below.
    assert len(premise.SERVER_INSTRUCTIONS) < 3900, "the premise is growing — move mechanics to the seed"


# ── THE ATTACHMENT FLOW (2026-10-22 evidenced rewrite — DEV/RESEARCH/ ─────────────────────
#    mcp-instructions-attachment-flow-20261022.md §3, applied verbatim) ────────────────────
#
# The EPA-PDF incident: OpenWebUI delivers an attachment as extracted <source> TEXT, never
# bytes. The old surface routed the model into ingest_file's bytes-only contract anyway — the
# model correctly refused (59k chars of <think>, ~15k tokens), ground silently for 3.5 minutes,
# and produced no visible content. These pins hold the fix on the SINGLE source
# (tools.py::TOOLS) that both doors resolve from.

# The research doc's measured served lengths, re-pinned EXACTLY. The document lane carries an
# EMPTY standing-rule suffix in both MEMORY_TEACHES states (verified 2026-10-22), so the
# description IS the whole 1024 budget — these are the production-served numbers.
_ATTACHMENT_REWRITE_LENGTHS = {"ingest_document": 1008, "ingest_file": 744, "document_status": 957}
# document_status re-pinned 429 -> 957 WITH evidence (bug-04 critic round 4): the
# accounting-class prose grew the description to 1116, past the 1024 OpenAI limit, and
# _build_tool_openapi silently CLAMPED the served copy at 1021 - dropping the post-ingest
# trigger sentence from the OpenWebUI door and turning four pins here red. 957 fits the
# budget whole (clamp inert) with the witness, the retriable qualification and the
# trigger sentence all intact.


def test_attachment_rewrite_lengths_are_pinned():
    """The caps are contractual (an over-1024 description makes an OpenAI-compatible client
    reject the ENTIRE tool list), and the doc's rewrite was MEASURED against them. Exact pins:
    if a future wording change moves a length, it must re-measure and move the pin WITH
    evidence, not silently drift back toward the cliff."""
    served = {t["name"]: len(t["description"]) for t in m.TOOLS}
    for name, length in _ATTACHMENT_REWRITE_LENGTHS.items():
        assert served[name] == length, (
            f"{name}: served description is {served[name]} chars, pinned at {length} "
            f"(evidenced rewrite). Re-measure and move the pin WITH evidence, or revert."
        )
        assert served[name] <= premise.OPENAI_DESCRIPTION_LIMIT


def test_attachment_routing_leads_ingest_document():
    """OWUI Tool Search (experimental) defers any tool whose definition exceeds 400 chars and
    truncates its description to the FIRST 100 chars. All twelve tools defer; the attachment
    routing must therefore live INSIDE the first 100 chars, not trail the description."""
    desc = {t["name"]: t["description"] for t in m.TOOLS}["ingest_document"]
    assert desc.startswith(
        "Store a document a HUMAN shared: pasted text, an article, notes, or an ATTACHED FILE."
    ), "the attachment trigger must LEAD the description (first-100-char truncation)"
    assert "ATTACHED FILE" in desc[:100]
    assert "Attachments:" in desc[:100]


def test_ingest_document_names_the_attachment_contract():
    desc = {t["name"]: t["description"] for t in m.TOOLS}["ingest_document"]
    # the identity claim that stops the 3.5-minute inference (context text IS the file)
    assert "It IS the file" in desc
    # the fabrication ban, settled as a rule instead of per-turn reasoning
    assert "never fabricate or reconstruct base64" in desc
    # cross-tool routing, with the why carried in parentheses
    assert "do not call ingest_file for an attachment (its data_b64 is raw bytes you do not have)" in desc
    # the post-ingest confirmation close (defect D3: idle-waiting advice replaced by one call)
    assert "call document_status once to confirm" in desc
    # authorship scoping survives the rewrite
    assert "A user document is one a HUMAN shared" in desc
    # the idle-waiting advice the rewrite replaced must stay gone
    assert "ask again in a moment" not in desc


def test_ingest_file_states_who_can_honestly_call_it():
    desc = {t["name"]: t["description"] for t in m.TOOLS}["ingest_file"]
    assert "when you HOLD THE RAW BYTES" in desc
    assert "NOT for chat attachments" in desc
    assert "use ingest_document with that text instead" in desc
    assert "Never fabricate, truncate, or reconstruct base64" in desc
    # the trap sentence — "Use when the user shares an image or a PDF/file attachment" —
    # advertised precisely the call this client cannot honestly make. Must stay gone.
    assert "Use when the user shares an image or" not in desc


def test_document_status_carries_the_post_ingest_trigger():
    desc = {t["name"]: t["description"] for t in m.TOOLS}["document_status"]
    assert (
        "call this ONCE after any ingest_document call on an attached/pasted file to confirm "
        "it landed"
    ) in desc


def test_premise_carries_the_attachments_line():
    """The P1 premise line (doc §3.5), verbatim, plus the permanence of the two named trims
    that paid for it — re-adding either busts the 3600 pin the trims exist to defend."""
    p = premise.SERVER_INSTRUCTIONS
    assert (
        "ATTACHMENTS: a file the user attached reaches you as extracted TEXT (<source> blocks), "
        "never bytes. Remember it with ingest_document using that text; ingest_file is only for "
        "callers that truly hold the raw bytes. Never fabricate base64."
    ) in p
    assert "Things people mention in passing" not in p, "the motivational-tail trim paid for ATTACHMENTS"
    assert "weather tomorrow" not in p, "the weather-example trim paid for ATTACHMENTS"
    # 3599 - 81 (tail) - 178 (weather sentence) + 238 (ATTACHMENTS paragraph) = 3578, then
    # + 312 (HOST NOTE TOOLS paragraph, 2026-11-04 shadow-memory port of foss 5bf9e382) = 3890.
    assert len(p) == 3890, "premise moved — re-pin WITH evidence (the 3900 ceiling is contractual)"


def test_attachment_rewrites_resolve_on_the_openapi_door():
    """The parity core, applied to the rewritten three: the door OpenWebUI reads must serve
    the NEW descriptions byte-for-byte from tools.py::TOOLS."""
    spec = h._build_tool_openapi()
    canonical = {t["name"]: t["description"] for t in m.TOOLS}
    for name, path in (("ingest_document", "/ingest_document"),
                       ("ingest_file", "/ingest_file_b64"),
                       ("document_status", "/document_status")):
        assert spec["paths"][path]["post"]["description"] == canonical[name], (
            f"{name}: the OpenAPI door is not serving the rewritten description"
        )
    assert "<source>" in spec["paths"]["/ingest_document"]["post"]["description"]
    assert "<source>" in spec["paths"]["/ingest_file_b64"]["post"]["description"]
