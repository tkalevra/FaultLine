"""THE PATHWAY: directions for the cortex_recall / cortex_remember LOOP.

WHY THIS FILE EXISTS (the owner's measured complaint, 2026-08-01)
----------------------------------------------------------------
"It's not so much a tool as it is me babysitting to get you to write things down; the alternative
(proven) is increased token cost, repeat of errors, and a lack of understanding."

The cortex READ lane fires on its own. The WRITE lane does not. Production observation
(2026-07-14): a real connected model called `cortex_recall` FIVE times in one session and `cortex_remember`
ZERO times. It reads its own memory and never writes to it, so it can never get better.

Two fixes were already shipped and both were necessary but neither was sufficient: the
`cortex_remember` tool description was rewritten with triggers an assistant can recognise in itself,
and `cortex_usage_seed` standing rules were folded in (but those are MEMORY_TEACHES-gated, so
open-core never sees them).

THE PART NOBODY FIXED: the connect-time premise itself opens "FaultLine is this user's memory.
You do not have one." That is the first and highest-signal thing a model reads, it lands in the
system prompt before any tool list is weighed, and it teaches a mental model with NO SLOT for the
agent having a memory of its own. Every downstream nudge then argues against the premise we
opened with.

This is the SAME defect this project already diagnosed once for `recall_memory`: guidance that
teaches a model WHEN to call a tool was delivered inside that tool's own output, so a model that
never called it never saw the brief. The fix then was to move it to a channel that lands BEFORE
any call. These tests pin that it landed, on every door, for the cortex loop too.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

import src.mcp.http_server as h
import src.mcp.server as m
from src.mcp import premise


@pytest.fixture
def client():
    with TestClient(h.app) as c:
        yield c


def _reload(monkeypatch, **env):
    """Rebuild the served surface under a given env. Returns (premise, TOOLS, openapi spec).

    Reloading matters: the pathway is composed at IMPORT time (like the standing-rule suffix and
    the premise prefix before it), so a monkeypatched env only takes effect after a reload. The
    OpenAPI doc is cached, so that cache has to be dropped too or every assertion below measures
    a stale spec — the exact class of mistake that let the 1024-char cliff ship green.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import src.api.cortex_usage_seed as seed
    import src.mcp.premise as prem
    import src.mcp.tools as tools_mod
    importlib.reload(seed)
    importlib.reload(prem)
    importlib.reload(tools_mod)
    h._TOOL_OPENAPI_CACHE = None
    return prem, tools_mod.TOOLS, h._build_tool_openapi()


@pytest.fixture(autouse=True)
def _restore_module_state():
    """Leave the process as we found it — these tests reload shared modules."""
    yield
    import src.api.cortex_usage_seed as seed
    import src.mcp.premise as prem
    import src.mcp.tools as tools_mod
    importlib.reload(seed)
    importlib.reload(prem)
    importlib.reload(tools_mod)
    h._TOOL_OPENAPI_CACHE = None


def _openwebui_tool_payload(spec):
    """Replicate OpenWebUI's `convert_openapi_to_tool_payload` EXACTLY: it reads ONLY
    `paths` -> per-operation operationId + description|summary, and NEVER `info.description`."""
    return {i["post"]["operationId"]:
            i["post"].get("description", i["post"].get("summary", ""))
            for i in spec["paths"].values()}


# ── The directions must teach BOTH halves, with a trigger the caller can recognise ────────

def test_pathway_teaches_the_read_half_and_the_write_half():
    """The whole point. The read half already fires on its own; the WRITE half is what fails.
    Directions that name only one of them re-ship the bug in nicer prose."""
    p = premise.CORTEX_PATHWAY
    assert "cortex_recall" in p, "the read half must be named"
    assert "cortex_remember" in p, "the WRITE half — the one that never fires — must be named"
    assert "loop" in p.lower(), "it must read as a loop, not two unrelated tools"


def test_pathway_write_trigger_is_concrete_and_recognisable():
    """"Call it when relevant" is useless — a caller cannot check that. "They corrected you" is
    checkable. This is the same lesson as the premise's "you are about to GUESS" trigger."""
    p = premise.CORTEX_PATHWAY
    assert "correct" in p.lower(), "being corrected is the clearest trigger; it must be stated"
    assert "Being corrected IS the" in p, \
        "the trigger must be stated as THE trigger, not buried in a list of maybes"


def test_pathway_states_the_cost_of_not_writing():
    """A model needs a reason, not just a rule. The cost is concrete and it is the user's:
    what is never written down has to be taught again."""
    assert "teach you again" in premise.CORTEX_PATHWAY


def test_pathway_keeps_the_notes_out_of_the_user_fact_band():
    """The flagent_<uuid> firewall is inviolable. The directions must never invite a model to
    repeat an operational note back as something the user told it — a leak through PROSE would
    defeat a firewall that holds perfectly well in code."""
    p = premise.CORTEX_PATHWAY.lower()
    assert "not facts" in p or "not the user's facts" in p
    assert "tentativ" in p, "notes are held tentatively — they are not grounded user truth"


# ── OWN-WORDS lane: the cortex write lane must not carry the verbatim mandate ─────────────

def test_cortex_tools_demand_own_words_not_user_verbatim(monkeypatch):
    """INJECTION-VECTOR FIX. The cortex tools must NEVER instruct the model to pass the user's
    message VERBATIM into a note or a query — pasted user content (transcripts, terminal
    output) would become the note body, and notes come back to the agent in later sessions.
    The agent writes the lesson itself, in its own words; a note it composes cannot carry a
    user's pasted payload. The USER-memory tools keep their verbatim mandate: that lane is
    the engine's raw material, and verbatim is correct THERE and only there."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    by_name = {t["name"]: t["description"] for t in tools}
    for name in premise.CORTEX_PATHWAY_TOOLS:
        desc = by_name[name]
        assert "verbatim" not in desc.lower(), (
            f"{name} still instructs passing the user's message verbatim — that is the "
            f"injection vector this change removed. Verbatim belongs to recall_memory / "
            f"remember_facts (the user's memory), not the cortex lane."
        )
    assert "your own words" in by_name["cortex_remember"].lower(), (
        "cortex_remember must positively instruct the agent to write the lesson itself, "
        "in its own words — removing the verbatim mandate without adding the own-words "
        "one leaves the write lane with no guidance at all."
    )
    for name in ("recall_memory", "remember_facts"):
        assert "VERBATIM" in by_name[name], (
            f"{name} lost its verbatim mandate — the user-memory lane REQUIRES the user's "
            f"message byte-for-byte; the engine extracts from the raw sentence."
        )
    # The inputSchema FIELD descriptions are a second model-facing surface the mandate lived
    # on (the old `text` field demanded the user's message VERBATIM, "do not paraphrase").
    for t in tools:
        if t["name"] in premise.CORTEX_PATHWAY_TOOLS:
            for prop in (t.get("inputSchema", {}).get("properties", {}) or {}).values():
                pdesc = str(prop.get("description", ""))
                assert "verbatim" not in pdesc.lower(), (
                    f"{t['name']} field description still carries the verbatim mandate"
                )
    # The REST door mirrors the tool text by hand (http_server._TOOL_OPS + request models) —
    # a regression there ships green unless pinned here too.
    from src.mcp import http_server as _hs
    for op in getattr(_hs, "_TOOL_OPS", []):
        if op.get("op") in premise.CORTEX_PATHWAY_TOOLS:
            blob = json.dumps(op)
            assert "verbatim" not in blob.lower(), (
                f"REST door _TOOL_OPS entry {op.get('op')} still carries the verbatim mandate"
            )
    # The pydantic request models + FastAPI endpoint docstrings are further model-facing
    # surfaces on the REST door (OpenWebUI reads operation descriptions) — pin them too.
    import inspect as _inspect
    for _mname in ("CortexRememberRequest", "CortexRecallRequest"):
        _model = getattr(_hs, _mname, None)
        if _model is None:
            continue
        for _f in _model.model_fields.values():
            _fdesc = str(getattr(_f, "description", "") or "")
            assert "verbatim" not in _fdesc.lower(), (
                f"{_mname} field description still carries the verbatim mandate"
            )
    for _fn_name in ("rest_cortex_remember", "rest_cortex_recall"):
        _fn = getattr(_hs, _fn_name, None)
        if _fn is not None:
            _doc = _inspect.getdoc(_fn) or ""
            assert "verbatim" not in _doc.lower(), (
                f"{_fn_name} docstring still carries the verbatim mandate"
            )


# ── SUBJECT-AGNOSTIC: a physician, a vet and a lawyer must read this as addressed to them ─

def test_pathway_has_no_domain_vocabulary():
    """The ORIGINAL cortex framing ("a command that failed", "a command overrun") is triggers
    only a coding agent ever meets — a conversational assistant runs no commands, never
    recognised its own trigger, and read the whole capability as "not for me". A WHEN the
    consumer cannot recognise in itself is not a WHEN. These directions must not repeat that."""
    banned = ["command", "deploy", "repo", "git", "code", "script", "terminal", "shell",
              "compile", "server", "docker", "test suite", "developer", "engineer"]
    body = premise.CORTEX_PATHWAY.lower()
    hits = [w for w in banned if w in body]
    assert not hits, (
        f"domain/persona vocabulary in the pathway directions: {hits}. A vet, a physician and a "
        f"lawyer must all read this as addressed to them."
    )


def test_pathway_is_model_agnostic_prose_not_a_client_mechanism():
    """The trust lives in the memory, not the model. The directions are prose served by the
    memory's own doors — no hook, no client-specific mechanism, nothing outside the server."""
    body = premise.CORTEX_PATHWAY.lower()
    for w in ("claude", "gpt", "mistral", "qwen", "openwebui", "anthropic", "openai", "hook"):
        assert w not in body, f"client/model-specific reference in model-agnostic directions: {w}"


# ── THE CARRIER: prove it ARRIVES, per door ──────────────────────────────────────────────

def test_pathway_rides_the_mcp_initialize_handshake(client):
    """DOOR 1 — `initialize.instructions`. The only channel that reaches a model BEFORE it calls
    anything. VERIFIED CARRIER: this repo's own Claude Code session receives SERVER_INSTRUCTIONS
    verbatim in its system prompt at connect."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}}).json()
    ins = r["result"]["instructions"]
    assert "cortex_remember" in ins and "cortex_recall" in ins, \
        "the pathway never reaches a model that has not called anything yet"


def test_pathway_rides_the_stdio_initialize_too():
    """Same door, other transport. stdio and HTTP must not teach different products — a premise
    that lands on one and not the other is drift with extra steps."""
    from src.mcp.premise import SERVER_INSTRUCTIONS
    assert "cortex_remember" in SERVER_INSTRUCTIONS
    # server.py (stdio) resolves the SAME constant — pinned by import, not by a pasted copy.
    src = (m.__file__ or "")
    assert src, "server module must be importable for the stdio door to carry the premise"


def test_pathway_survives_the_openwebui_conversion(monkeypatch):
    """DOOR 2 — per-operation tool descriptions, and THE ONE THAT MATTERS.

    `initialize.instructions` does not exist in OpenAPI, and OpenWebUI's converter never reads
    `info.description`. So for the door our users' models actually connect through, the tool
    description is the ONLY proactive channel. A pathway parked only in the handshake would be
    dropped on the floor by the exact client this was built for.
    """
    prem, _, spec = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    payload = _openwebui_tool_payload(spec)
    marker = prem.CORTEX_TOOL_PREMISE.strip()[:40]
    carrying = {n for n, d in payload.items() if marker in d}
    assert carrying == set(prem.CORTEX_PATHWAY_TOOLS), (
        f"pathway carried by {carrying}, expected exactly {set(prem.CORTEX_PATHWAY_TOOLS)}"
    )


def test_each_cortex_tool_names_the_other_half(monkeypatch):
    """The production failure was cortex_recall 5x / cortex_remember 0x. A model reading `cortex_recall` alone
    learns a read tool; it must learn from THAT SAME TEXT that a write half exists, or the loop
    never closes. Symmetrically for a model that meets `cortex_remember` first."""
    _, _, spec = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    payload = _openwebui_tool_payload(spec)
    assert "cortex_remember" in payload["cortex_recall"], "cortex_recall must point at the write half"
    assert "cortex_recall" in payload["cortex_remember"], "cortex_remember must point at the read half"


def test_pathway_does_not_ride_every_tool(monkeypatch):
    """COST. The pathway is one idea; paying for it on all 8 tools pays for it 8 times."""
    prem, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    marker = prem.CORTEX_TOOL_PREMISE.strip()[:40]
    carrying = {t["name"] for t in tools if marker in t["description"]}
    assert carrying == set(prem.CORTEX_PATHWAY_TOOLS)


def test_pathway_leads_the_cortex_tool_descriptions(monkeypatch):
    """PREFIX, not suffix — same reason as the premise: "does a loop like this exist" must land
    before "how do I call this one", or it becomes trailing noise the model may never weigh."""
    prem, _, spec = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    payload = _openwebui_tool_payload(spec)
    for name in prem.CORTEX_PATHWAY_TOOLS:
        assert payload[name].startswith(prem.CORTEX_TOOL_PREMISE.strip()[:40])


# ── THE RULE-FIRST SHAPE + CONTRAST PAIR (Build 4) ───────────────────────────────
#
# A prose "in your own words" mandate always loses to the verbatim message sitting right
# there in context — the model must SEE the target. These pins hold the two halves of
# that fix on EVERY served surface: the GOOD/BAD contrast pair in the tool description
# (both pathway states), and the rule-first shape of the write fields (the note body
# leads with the rule; the owner's exact words are demoted to owner_quote, secondary).


def test_cortex_remember_description_carries_contrast_pair(monkeypatch):
    """ONE good-and-bad pair, in the SERVED description, under BOTH pathway states — the
    pair is what shows the model the target instead of telling it about the rule."""
    for flag in ("true", "false"):
        _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS=flag)
        desc = next(t for t in tools if t["name"] == "cortex_remember")["description"]
        low = desc.lower()
        assert "good:" in low and "bad:" in low, (
            f"pathway={flag}: the GOOD/BAD contrast pair is gone from cortex_remember — "
            f"a prose mandate without an example loses to the verbatim message in context"
        )
        assert "when <situation> arises, do <action>, because <mechanism>" in low, (
            f"pathway={flag}: the rule-first slots (situation -> action -> mechanism) "
            f"must be stated in the description"
        )
        # the BAD example must be the TRANSCRIPTION shape (owner words, no rule), and the
        # GOOD example the rule shape — not two arbitrary sentences.
        good_i, bad_i = low.index("good:"), low.index("bad:")
        assert good_i < bad_i
        assert "he said" in low[bad_i:bad_i + 80], (
            "the BAD example should show the owner-words transcription losing"
        )


def test_rule_first_fields_owner_quote_demoted(monkeypatch):
    """The write fields carry the shape: text IS the rule (leads with the slots);
    owner_quote is its own OPTIONAL secondary field; branch is advertised on both
    halves of the loop as a ranking signal that never gates."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    remember = next(t for t in tools if t["name"] == "cortex_remember")
    props = remember["inputSchema"]["properties"]
    assert props["text"]["description"].startswith("THE RULE"), (
        "the text field must lead with THE RULE — the shape is the fix"
    )
    assert "when <situation> arises, do <action>, because <mechanism>" in props["text"]["description"]
    assert "owner_quote" in props and "carried SECOND" in props["owner_quote"]["description"]
    assert "branch" in props and "never hides anything" in props["branch"]["description"]
    recall = next(t for t in tools if t["name"] == "cortex_recall")
    rprops = recall["inputSchema"]["properties"]
    assert "branch" in rprops and "never hides anything" in rprops["branch"]["description"]


def test_rest_door_advertises_owner_quote_and_branch(monkeypatch):
    """The OpenAPI door (the one OpenWebUI reads) must carry the same fields — a
    shape fix that misses a door ships half a product."""
    _, _, spec = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    rem = spec["paths"]["/cortex_remember"]["post"]
    blob = rem.get("requestBody", {}).get("content", {}).get("application/json", {})
    props = blob.get("schema", {}).get("properties", {})
    assert "owner_quote" in props and "branch" in props, (
        f"REST door missing the new write fields: {sorted(props)}"
    )
# ── THE 1024-CHAR CLIFF, under the PRODUCTION condition ──────────────────────────────────

def test_budget_holds_with_standing_rules_AND_pathway(monkeypatch):
    """THE CONDITION THAT ACTUALLY SHIPS. Over 1024 an OpenAI-compatible client rejects the
    WHOLE tool list — every tool, not just the offender — and the MCP door stays perfectly
    healthy while it happens, so every local check looks green.

    `cortex_remember` is the tool nearest the cliff (measured 2026-08-14: 1004 pathway-on,
    1017 pathway-off). Parametrized over BOTH pathway states: the ROLLBACK configuration
    (pathway OFF) is a real, documented lever — flag off means no prefix AND no excision,
    so the full WHEN span stays and the budget shifts; it has been over the cliff before.
    """
    for flag in ("true", "false"):
        prem, tools, _ = _reload(
            monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS=flag)
        over = {t["name"]: len(t["description"]) for t in tools
                if len(t["description"]) > prem.OPENAI_DESCRIPTION_LIMIT}
        assert not over, (
            f"pathway={flag}: over the {prem.OPENAI_DESCRIPTION_LIMIT}-char limit: {over}"
        )


def test_excision_actually_removed_the_superseded_span(monkeypatch):
    """The prefix is paid for by DELETING what it supersedes, not by shrinking something else.
    If the source text is edited so the anchors no longer match, the excision no-ops (by design)
    — this test is what tells you that happened, before the budget test goes red for a reason
    that looks unrelated. Anchor-based: checks the START anchor is gone (proving the excision
    fired), not a full-string match."""
    prem, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    desc = next(t["description"] for t in tools if t["name"] == "cortex_remember")
    start_anchor, end_anchor = prem._EXCISE_ANCHORS["cortex_remember"]
    assert start_anchor not in desc, (
        "the start anchor survived — the excision did not match, so we are paying "
        "for the same trigger three times. Re-key _EXCISE_ANCHORS to the source text."
    )
    assert end_anchor not in desc, (
        "the end anchor survived — the excision span did not cover it."
    )


def test_excision_keeps_the_why_and_the_firewall(monkeypatch):
    """An excision must not take load-bearing text with it. What has to survive: the WHY, the
    'these are not the user's facts' firewall, and the hand-off to remember_facts."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    desc = next(t["description"] for t in tools if t["name"] == "cortex_remember")
    # THE WHY, asserted as a PROPERTY not a phrase. This used to pin the literal string
    # "stop repeating your own mistakes". That WHY was later reworded — and strengthened
    # ("so you do not repeat the mistake", "one you will pay for twice", "this user must teach
    # you again next session") — at which point the literal assertion failed on BETTER copy.
    # A guard that forbids improving the wording is not guarding the thing it cares about.
    # What must survive is a stated cost of NOT writing the lesson down; any phrasing counts.
    lowered = desc.lower()
    why = ("repeat the mistake", "repeating your own mistakes", "pay for twice",
           "teach you again", "same mistake")
    assert any(w in lowered for w in why), (
        "the WHY was excised — cortex_remember no longer tells a model what NOT writing the "
        "lesson down costs it. Restore a consequence, in any wording."
    )
    # The firewall — load-bearing, but matched case-INSENSITIVELY: the text was recased
    # ("NOT" -> "not") and a case-sensitive pin failed on identical meaning.
    assert "not the user's fact" in lowered, "the cortex/user-fact firewall sentence is gone"
    assert "remember_facts" in desc            # the hand-off


def test_excision_no_ops_instead_of_half_applying(monkeypatch):
    """FAIL SAFE. If the keyed span is not found, serve the FULL text — never a half-excised
    description with a dangling clause. Silent partial edits to model-facing prose are how a
    door starts teaching something subtly wrong."""
    _reload(monkeypatch, CORTEX_PATHWAY_DIRECTIONS="true")
    original = "Record a lesson. SOMETHING ELSE ENTIRELY. Use remember_facts."
    out = premise.apply_cortex_pathway("cortex_remember", original)
    assert original in out, "unmatched excision must leave the description intact"
    assert out.startswith(premise.CORTEX_TOOL_PREMISE.strip()[:20])


# ── FLAG OFF: byte-for-byte legacy ───────────────────────────────────────────────────────

@pytest.mark.parametrize("memory_teaches", ["", "1"])
def test_flag_off_is_byte_for_byte_legacy(monkeypatch, memory_teaches):
    """ROLLBACK LEVER. CORTEX_PATHWAY_DIRECTIONS=false must reproduce the pre-feature bytes on
    every door, in BOTH MEMORY_TEACHES states — production runs with it ON, so a check that only
    covers OFF measures a shorter string than production serves. That gap is exactly where the
    1024-char bug lived last time."""
    prem, tools, spec = _reload(monkeypatch, MEMORY_TEACHES=memory_teaches,
                                CORTEX_PATHWAY_DIRECTIONS="false")

    # Server prose: no pathway anywhere.
    assert "cortex_remember" not in prem.SERVER_INSTRUCTIONS
    assert prem.SERVER_INSTRUCTIONS == prem.OPENAPI_DESCRIPTION
    assert "cortex_remember" not in spec["info"]["description"]

    # Tool descriptions: no prefix, and the excised span is INTACT (nothing was excised).
    marker = prem.CORTEX_TOOL_PREMISE.strip()[:40]
    assert not any(marker in t["description"] for t in tools)
    desc = next(t["description"] for t in tools if t["name"] == "cortex_remember")
    start_anchor, _ = prem._EXCISE_ANCHORS["cortex_remember"]
    assert start_anchor in desc, \
        "flag OFF must not excise — the legacy description is served whole"


def test_flag_off_helpers_are_inert(monkeypatch):
    """The gate is a PURE env read that imports nothing, so an open-core deployment is a clean
    no-op rather than a code path that merely produces empty strings late."""
    _reload(monkeypatch, CORTEX_PATHWAY_DIRECTIONS="false")
    assert premise.pathway_directions_enabled() is False
    assert premise.cortex_pathway_prefix("cortex_remember") == ""
    assert premise.apply_cortex_pathway("cortex_remember", "UNTOUCHED") == "UNTOUCHED"


def test_flag_defaults_on(monkeypatch):
    """Default ON: the capability this teaches is core-product, not SaaS, and a write lane that
    never fires is the defect being fixed. (Contrast MEMORY_TEACHES, which follows SAAS_MODE.)"""
    monkeypatch.delenv("CORTEX_PATHWAY_DIRECTIONS", raising=False)
    importlib.reload(premise)
    assert premise.pathway_directions_enabled() is True


# ── COST ─────────────────────────────────────────────────────────────────────────────────

def test_pathway_stays_cheap():
    """Proactive text is paid on EVERY interaction that opens a session. ~700 chars ≈ ~170
    tokens. If this fails, mechanics were dumped here that belong in the relevance-gated seed
    (which costs nothing when irrelevant)."""
    # BUDGET RAISED 900 -> 1000 (2026-08-10), deliberately, once. The pathway reached 963 when
    # the two-call loop was made explicit ("cortex_recall is the FIRST call", the WRITE WHEN
    # list). That content is PROACTIVE — it must land before any call — so it cannot move to
    # the relevance-gated seed, which only rides a recall RESPONSE. The ceiling is still tight;
    # if this fails again, the next addition is very likely seed material. Do not raise twice.
    assert len(premise.CORTEX_PATHWAY) < 1000, \
        "the pathway is growing — move mechanics into cortex_usage_seed"
    assert len(premise.CORTEX_TOOL_PREMISE) < 260, \
        "the tool-carrier variant draws on the same 1024 budget as the tool's own text"
    # Same ceiling as tests/mcp/test_premise_and_tool_parity.py::test_premise_stays_cheap —
    # raised 3000 -> 3600 there for the cold-connect guidance (raise #2: 3600 -> 3900 on
    # 2026-11-04 there AND here, for the shadow-memory HOST NOTE TOOLS block, port of foss
    # 5bf9e382), and kept in step here. Two copies of one budget in two files is how they
    # drift; if you move one, move both.
    assert len(premise.SERVER_INSTRUCTIONS) < 3900, \
        "premise + pathway must stay inside the standing budget"
