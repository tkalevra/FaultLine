"""THE USER-MEMORY WRITE LANE IS DEFINED BY AUTHORSHIP, NOT CONTENT.

WHY THIS FILE EXISTS (production incident 2026-08-14)
-----------------------------------------------------
A build agent connected over MCP passed its OWN operating brief — engineering prose about
brain-rate governance — into ``remember_facts`` and ``recall_memory`` VERBATIM. It was
OBEYING the contract as written: "pass the user's message VERBATIM" with nothing separating
a HUMAN's conversational turn from the AGENT's own working text. The damage on the owner's
seat: 21 durable Class-A facts at confidence 1.0 with junk entities ("cerebras free tier"
typed Object, "50" as an entity, "wedged_failopen"), plus 7 engine-grown rel_types
(log/permit/evidence_by/see/allow/hit/real_tier_key) minted from the junk.

THE FIX THESE TESTS PIN
-----------------------
The write lane is defined by WHO authored the text:

  * remember_facts / ingest_document accept ONLY a human being's own conversational turn
    (or a document a human shared), verbatim — the agent's own working text NEVER qualifies,
    no matter what it looks like.
  * recall_memory carries a HUMAN's question; operational history (commits, builds, deploys,
    logs, past work sessions) routes to cortex_recall.
  * What the agent LEARNS while working goes to cortex_remember — its own memory, which
    exists precisely so the user lane never has to absorb it.

TWO THINGS THIS FILE REFUSES TO LET HAPPEN AGAIN:

  1. WEAKENING HUMAN CAPTURE. The authorship scoping must not soften the verbatim mandate,
     the "even in passing" trigger, or the no-permission default on the user lane — a fix
     that filters genuine human turns would be worse than the disease.
  2. OVERCORRECTING INTO THE CORTEX LANE. The cortex tools must NOT gain a verbatim mandate
     (that is the injection vector the own-words rewrite removed — pinned already in
     test_cortex_pathway_directions.py; re-pinned here from the authorship angle so this
     file stands alone).

AND ONE THING IT FORBIDS BY DESIGN: content-based heuristics. The discriminator is
authorship — "looks technical" rules would also filter a human engineer's genuine turn.
The descriptions must scope by WHO spoke, never by what the text resembles.
"""
import importlib

import pytest

import src.mcp.http_server as h
from src.mcp import premise


def _reload(monkeypatch, **env):
    """Rebuild the served surface under a given env. Returns (premise, TOOLS, openapi spec).

    Mirrors tests/mcp/test_cortex_pathway_directions.py::_reload: the premise prefix, the
    standing-rule suffix, and the pathway layer are all composed at IMPORT time, so a
    monkeypatched env only takes effect after a reload — and the OpenAPI doc is cached, so
    that cache has to be dropped too or every length assertion below measures a stale spec
    (the exact class of mistake that let the 1024-char cliff ship green).
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


def _tool(tools, name):
    return next(t for t in tools if t["name"] == name)


# ── remember_facts: the human turn, verbatim; NEVER the agent's own working text ──────────

def test_remember_facts_describes_the_human_turn(monkeypatch):
    """The lane is defined by WHO authored the text. The description must say positively
    that the input is a HUMAN being's conversational turn — not merely 'a message' (the
    unscoped word that let a build agent file its own brief as user fact)."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    desc = _tool(tools, "remember_facts")["description"]
    lowered = desc.lower()
    human_framing = ("human" in lowered) or ("person" in lowered and "said" in lowered)
    assert human_framing, (
        "remember_facts no longer frames its input as a HUMAN's words — the 2026-08-14 "
        "incident happened because the contract let ANY text qualify as 'the user's message'."
    )


def test_remember_facts_keeps_the_verbatim_mandate(monkeypatch):
    """AUTHORSHIP SCOPING MUST NOT WEAKEN HUMAN CAPTURE. The verbatim mandate — the engine's
    raw material — stays exactly as strong as before the fix, for genuine human turns."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    desc = _tool(tools, "remember_facts")["description"]
    assert "VERBATIM" in desc, "the verbatim mandate was dropped from remember_facts"
    lowered = desc.lower()
    assert "even in passing" in lowered, "the even-in-passing trigger was dropped"
    assert "permission" in lowered, "the no-permission-needed default was dropped"


def test_remember_facts_forbids_the_agents_own_working_text(monkeypatch):
    """The never-clause: the agent's task brief, plan, analysis, or tool output NEVER
    qualifies as the user's message — and operational lessons route to cortex_remember."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    desc = _tool(tools, "remember_facts")["description"].lower()
    assert "your own working text" in desc, (
        "remember_facts must name the agent's own working text as NEVER qualifying — "
        "authorship, not content shape, is the discriminator."
    )
    assert "cortex_remember" in desc, (
        "the hand-off is load-bearing: operational knowledge the agent learns while working "
        "must be routed to cortex_remember, or it has no home and lands here again."
    )


def test_remember_facts_text_field_carries_the_same_scoping(monkeypatch):
    """The inputSchema field description is a second model-facing surface — the old one
    demanded 'the user's message' unscoped. Both surfaces must carry the authorship line."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    t = _tool(tools, "remember_facts")
    fdesc = t["inputSchema"]["properties"]["text"]["description"].lower()
    assert "human" in fdesc, "the text field must scope input to a HUMAN's conversational turn"
    assert "verbatim" in fdesc, "the text field must keep the verbatim mandate"
    assert "your own working text" in fdesc or "working text" in fdesc, (
        "the text field must forbid the agent's own working text"
    )


def test_remember_facts_schema_has_no_evidence_property(monkeypatch):
    """The `evidence` property was (a) cortex coaching on the wrong lane, and (b) BROKEN:
    the handler remember_facts_tool(text, user_id) takes no evidence param, so a model
    obeying the schema got a TypeError and the whole write was silently dropped. It must
    stay off this schema — its home is cortex_remember (pinned below)."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    props = _tool(tools, "remember_facts")["inputSchema"]["properties"]
    assert "evidence" not in props, (
        "evidence is back on remember_facts — its handler cannot accept it (silent "
        "TypeError drops the write) and the coaching belongs to the cortex lane."
    )
    desc = _tool(tools, "remember_facts")["description"]
    blob = str(_tool(tools, "remember_facts")["inputSchema"])
    for coaching in ("bind the claim", "rots into a trap", "merge-base"):
        assert coaching not in desc.lower(), (
            f"cortex evidence coaching ({coaching!r}) leaked back into remember_facts"
        )
        assert coaching not in blob.lower(), (
            f"cortex evidence coaching ({coaching!r}) leaked back into the remember_facts schema"
        )


# ── cortex_remember: the evidence property's TRUE home ───────────────────────────────────

def test_cortex_remember_schema_advertises_evidence(monkeypatch):
    """The pair was swapped between the two tools: cortex_remember_tool has accepted an
    `evidence: dict | None` param all along (server.py) while its schema never advertised
    it — and remember_facts advertised cortex's coaching instead. Pin the corrected pair."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    props = _tool(tools, "cortex_remember")["inputSchema"]["properties"]
    assert "evidence" in props, (
        "cortex_remember must advertise the evidence property its handler already accepts — "
        "it was swapped onto remember_facts, whose handler TypeErrors on it."
    )
    edesc = str(props["evidence"].get("description", "")).lower()
    assert "sha" in edesc or "proof" in edesc, (
        "the evidence field must carry the cortex semantics: bind a lesson to its proof."
    )
    # And the handler really does accept it — otherwise the schema lies in the other dir.
    import inspect
    import src.mcp.server as srv
    sig = inspect.signature(srv.cortex_remember_tool)
    assert "evidence" in sig.parameters, (
        "cortex_remember_tool no longer accepts evidence — the schema would be advertising "
        "a param the handler drops."
    )


# ── recall_memory: operational history routes to the cortex lane ──────────────────────────

def test_recall_memory_routes_operational_history_to_cortex(monkeypatch):
    """The class that actually pollutes this lane is operational history — the build agent's
    commits, builds, logs, rate limits, past work sessions. 'Do not call for a task' was too
    weak; the description must NAME that class and route it to cortex_recall."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    lowered = _tool(tools, "recall_memory")["description"].lower()
    assert ("commits" in lowered) or ("operational history" in lowered), (
        "recall_memory must name operational history as the excluded class"
    )
    assert "cortex_recall" in lowered, (
        "the routing must name the destination tool, not merely exclude the input"
    )


def test_recall_memory_keeps_verbatim_and_the_human_query_scope(monkeypatch):
    """The query is authorship-scoped (a HUMAN's message) and the verbatim mandate for it
    must survive the routing change — human capture is not negotiable."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    desc = _tool(tools, "recall_memory")["description"]
    assert "VERBATIM" in desc, "recall_memory lost its verbatim mandate for the user's query"
    assert "human" in desc.lower(), "recall_memory must scope the query to a HUMAN asking"


# ── ingest_document: user-shared documents only ───────────────────────────────────────────

def test_ingest_document_carries_user_shared_framing(monkeypatch):
    """A document the USER shared — never the agent's own working text, plans, or session
    output. Same incident class, third entry point."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    lowered = _tool(tools, "ingest_document")["description"].lower()
    assert "human" in lowered, "ingest_document must scope documents to what a HUMAN shared"
    assert "your own working" in lowered, (
        "ingest_document must forbid the agent's own working text"
    )


# ── The premise carries the authorship clause on every door ───────────────────────────────

@pytest.mark.parametrize("pathway", ["true", "false"])
def test_server_instructions_carry_the_authorship_clause(monkeypatch, pathway):
    """SERVER_INSTRUCTIONS (the connect-time premise, mirrored to every door) must scope the
    remember_facts trigger to the user's OWN words in conversation — and hand the agent's
    working lessons to cortex_remember, which the pathway paragraph below it introduces."""
    prem, _, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS=pathway)
    p = " ".join(prem.SERVER_INSTRUCTIONS.split())  # normalize hard-wrapped lines
    lowered = p.lower()
    assert "their sentence verbatim" in lowered or "own words in conversation" in lowered, (
        "the WHEN bullet must scope remember_facts to the user's own words in conversation"
    )
    assert "never your working text" in lowered, (
        "the premise must exclude the agent's working text from the user-memory lane"
    )
    assert "cortex_remember" not in lowered or pathway == "true", (
        "the base premise must not name cortex_remember — the flag-off legacy pin requires "
        "its absence (the pathway paragraph below carries the routing when enabled)"
    )
    # Third copy of the premise ceiling (see test_premise_and_tool_parity.py::test_premise_stays_cheap
    # and test_cortex_pathway_directions.py) — raise #2 3600 -> 3900 (2026-11-04, shadow-memory
    # HOST NOTE TOOLS block, port of foss 5bf9e382). Keep all three copies in step.
    assert len(prem.SERVER_INSTRUCTIONS) < 3900, "premise + pathway must stay inside the standing budget"


# ── THE 1024-CHAR CLIFF: every tool, both pathway states, standing rules ON ───────────────

@pytest.mark.parametrize("pathway", ["true", "false"])
def test_served_budget_holds_for_every_tool_both_pathway_states(monkeypatch, pathway):
    """Over 1024 an OpenAI-compatible client rejects the WHOLE tool list. The authorship
    additions were paid for by compressing the same descriptions — this proves the
    compensation actually happened, in BOTH the production and the rollback configuration,
    with MEMORY_TEACHES on (the condition that actually ships)."""
    prem, tools, spec = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS=pathway)
    over = {t["name"]: len(t["description"]) for t in tools
            if len(t["description"]) > prem.OPENAI_DESCRIPTION_LIMIT}
    assert not over, (
        f"pathway={pathway}: over the {prem.OPENAI_DESCRIPTION_LIMIT}-char limit: {over}. "
        f"An OpenAI-compatible client will reject the ENTIRE tool list."
    )
    # The OpenAPI door serves the same descriptions — clamp and parity included.
    for item in spec["paths"].values():
        assert len(item["post"]["description"]) <= prem.OPENAI_DESCRIPTION_LIMIT


# ── THE SPLIT, guarded in both directions ─────────────────────────────────────────────────

def test_user_lane_not_forbidden_verbatim_cortex_lane_still_own_words(monkeypatch):
    """TWO-SIDED REGRESSION GUARD. (a) The user-lane tools must NOT have picked up any
    'do not pass verbatim' phrasing — human capture stays byte-for-byte. (b) The cortex
    tools must NOT have gained a verbatim mandate from this fix — that is the injection
    vector the own-words rewrite removed; notes the agent composes itself cannot carry a
    pasted payload."""
    _, tools, _ = _reload(monkeypatch, MEMORY_TEACHES="1", CORTEX_PATHWAY_DIRECTIONS="true")
    by_name = {t["name"]: t for t in tools}
    for name in ("remember_facts", "recall_memory", "ingest_document"):
        lowered = by_name[name]["description"].lower()
        assert "never verbatim" not in lowered and "not verbatim" not in lowered, (
            f"{name} forbids verbatim — the user lane REQUIRES the human's words verbatim"
        )
    for name in premise.CORTEX_PATHWAY_TOOLS:
        lowered = by_name[name]["description"].lower()
        assert "verbatim" not in lowered, (
            f"{name} gained a verbatim mandate — that is the cortex injection vector; "
            f"the agent writes its own notes in its own words."
        )
        for prop in (by_name[name].get("inputSchema", {}).get("properties", {}) or {}).values():
            assert "verbatim" not in str(prop.get("description", "")).lower(), (
                f"{name} field description gained the verbatim mandate"
            )
