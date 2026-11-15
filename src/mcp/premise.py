"""THE PREMISE — what a connecting model is told FaultLine is FOR, before it calls anything.

WHY THIS MODULE EXISTS
----------------------
Every tool we ship already explains HOW to call it, and most explain WHEN. None of them ever
explained WHY a model would want to — what FaultLine *is*, and what problem it solves *for the
model*. A model connected to us received eight function signatures and no premise, and had to
reverse-engineer the product from the word "memory". Real report from a real connected model:
"wtf is this and how does it help me". That is OUR brittleness, not weak tool-calling.

THE CHICKEN-AND-EGG THIS FIXES
------------------------------
We already had a teaching layer: the ``HOW YOU OPERATE`` channel (``cortex_usage_seed``,
surfaced by ``recall_usage_patterns``). But it rides the RECALL RESPONSE — it only reaches a
model that has ALREADY called ``recall_memory``. The guidance that teaches a model when to call
the tool was being delivered inside that tool's own output. A model that does not yet know why
it would ever call recall never sees the brief telling it when to call recall.

So that layer is REACTIVE and stays reactive. This one is PROACTIVE: it lands at CONNECT time,
before any tool call, which is the only place that can reach a model that has not called
anything yet. The two are complements, not duplicates — keep the mechanics (user-lane verbatim
passing, correction handling, tiering) in the seeded rules, and keep the PREMISE here.

TRANSPORT PARITY (load-bearing)
-------------------------------
The premise is defined ONCE, here, and consumed by every door:
  * MCP JSON-RPC ``initialize`` -> ``instructions``   (http_server.py, and server.py for stdio)
  * OpenAPI ``info.description``                      (what OpenWebUI reads off the tool server)
  * ``/v1`` hand-curated ``info.description``         (api_v1.py)
Pasting this copy per-transport is how the doors drift and one of them silently keeps telling
models nothing. If you add a door, import from here.

BUDGET: this lands in the model's context once per session. It is deliberately tight and
high-signal. Resist growing it — every line here is rented, not owned. Mechanics belong in the
seeded operating rules, which are relevance-gated and cost nothing when irrelevant.
"""

import os

# ── Protocol version negotiation (MCP lifecycle spec) ─────────────────────────────────────
#
# The spec is explicit: "If the server supports the requested protocol version, it MUST respond
# with the same version. Otherwise, the server MUST respond with another protocol version it
# supports. This SHOULD be the latest version supported by the server."
#
# We used to HARDCODE "2025-03-26" into the initialize result and ignore what the client asked
# for. A client that speaks 2025-06-18 got told 2025-03-26 and — per the spec — "SHOULD
# disconnect" if it does not support what we named. We were pinning every client to a version
# a year stale for no reason: our surface is tools-only, which is unchanged across these
# revisions.
#
# Newest first. Echo the client's version when we know it; otherwise offer our latest.
# OpenAI-compatible function-calling rejects a tool `description` longer than this. The MCP /
# Anthropic door has no equivalent cap — which is exactly why this bit us: the long canonical
# descriptions were harmless on the door we were watching and fatal on the door our users are on.
# Enforced at the serving seam (_build_tool_openapi) and pinned by a test.
OPENAI_DESCRIPTION_LIMIT = 1024

# 2026-07-28 is the STATELESS revision: no initialize/notifications/initialized handshake, no
# Mcp-Session-Id, every request self-contained with version+capabilities in `_meta`, plus the
# new `server/discover` RPC. We are DUAL-ERA: legacy clients (2025-11-25 and earlier) still send
# initialize; modern clients (2026-07-28) send per-request `_meta`. Both are served.
#
# 2025-11-25 and 2024-10-07 are the revisions the official MCP TypeScript SDK proposes
# (LATEST_PROTOCOL_VERSION and the fallback the SDK's supported-version whitelist will accept).
# 2026-08-10 incident: opencode 1.18.15's bundled SDK proposed 2025-11-25, we were not listing
# it, and `negotiate_protocol_version` "helpfully" echoed LATEST (2026-07-28) instead — a custom
# stateless revision the SDK has never seen, so its `supported.includes(echo)` check threw
# "protocol version is not supported" and the client's SSE fallback surfaced the real error as a
# misleading "Non-200 status code (405)". These two entries close that class of breakage: the
# SDK's own handshake is now served verbatim, and the echoed value is one the SDK re-sends in the
# `mcp-protocol-version` header on every subsequent request, which http_server.py validates
# against this same set.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
)
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# ── 2026-07-28 error-code allocation (SEP-2575) ──────────────────────────────────────────
# The spec partitions the JSON-RPC server-error range: -32000..-32019 stays implementation-defined
# (grandfathered SDK usage), -32020..-32099 is reserved for the MCP specification. Three codes
# previously sat in the implementation-defined range and were renumbered. Define them as named
# constants so both transports (http_server.py, server.py) agree on the values without literals.
ERROR_HEADER_MISMATCH = -32020
ERROR_MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
ERROR_UNSUPPORTED_PROTOCOL_VERSION = -32022


def decode_header_value(value: str) -> str:
    """Decode the spec's Base64 sentinel so a header can be compared to the body.

    A tool name or resource URI that is not safely representable as an ASCII header value
    travels as ``=?base64?<b64>?=`` (SEP-2243, Value Encoding). Comparing that wrapper
    against the raw body value would fail every non-ASCII name, so the wrapper is removed
    before the comparison. A malformed payload is returned verbatim — it will simply not
    match, which is the correct outcome for a header we cannot read.
    """
    if not value.startswith("=?base64?") or not value.endswith("?="):
        return value
    import base64
    try:
        return base64.b64decode(value[len("=?base64?"):-len("?=")]).decode("utf-8")
    except Exception:
        return value


def negotiate_protocol_version(requested: str | None) -> str:
    """Echo `requested` if we support it; else offer the newest revision we support that is
    NOT newer than what the client proposed. Never raises.

    The "not newer than requested" cap is the 2026-08-10 fix: our all-caps LATEST (2026-07-28)
    is a custom stateless revision only our modern clients know. If a client proposes something
    we do not list (e.g. the official SDK's next revision prior to us adopting it), echoing
    LATEST hands back a revision the client's own whitelist rejects and the handshake dies.
    All our revisions are ISO date strings, so lexicographic comparison is chronological.
    """
    if requested:
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            return requested
        for version in SUPPORTED_PROTOCOL_VERSIONS:  # newest first
            if version <= requested:                  # newest one the client can parse
                return version
        if requested < SUPPORTED_PROTOCOL_VERSIONS[-1]:
            return SUPPORTED_PROTOCOL_VERSIONS[-1]    # older than we serve: oldest we know
    return LATEST_PROTOCOL_VERSION


# Cacheability hint (2026-07-28 CacheableResult, SEP-2549): tools/list and server/discover are
# stable per-deployment (the tool set only changes on deploy/serve-mode flip), so a long TTL with
# "public" scope lets clients and intermediaries cache the catalog. Conservative — a value-flip
# just means the client holds a stale list for the TTL, and serve-mode already re-decides at call.
_LIST_TTL_MS = 300_000
_LIST_CACHE_SCOPE = "public"


def discover_result() -> dict:
    """The 2026-07-28 ``server/discover`` payload — one source, both transports.

    Advertises our supported protocol versions, capabilities, identity, and the same premise
    ``instructions`` every other door carries (transport parity: a model that opens with
    server/discover must learn the same FaultLine as one that opens with initialize or OpenAPI).
    """
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}, "prompts": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": {
            "name": "faultline-mcp", "version": "1.0.0"}},
        "instructions": SERVER_INSTRUCTIONS,
        "ttlMs": _LIST_TTL_MS,
        "cacheScope": _LIST_CACHE_SCOPE,
    }


# ── The premise, model-facing ─────────────────────────────────────────────────────────────
#
# Written in the SECOND PERSON, addressed to the model. It answers, in order:
#   1. What is this?          -> the user's memory; you do not have one
#   2. Why do you want it?    -> it is grounded truth, not your reconstruction
#   3. When do you reach?     -> concrete, checkable triggers (incl. "you are about to guess")
#   4. What is it NOT?        -> it does not browse or fetch; it answers the missing fact
#
# (3) is the one that changes behaviour. "Call it when relevant" is useless — a model cannot
# check that. "You are about to say 'remind me…' — that is the signal" is checkable.
#
# AUTHORSHIP LINE (production incident 2026-08-14): a build agent passed its OWN operating
# brief into remember_facts verbatim — the old contract said "their sentence verbatim" without
# separating a human's conversational turn from the agent's working text, and 21 junk Class-A
# facts landed on the owner's seat. The WHEN bullet and the VERBATIM paragraph below now scope
# the lane by WHO authored the words: the user's OWN turn, verbatim, always; the agent's
# working text never (its operational lessons go to cortex_remember). The discriminator is
# authorship, NOT content shape — no "looks technical" heuristics, which would weaken capture
# of genuine human turns. Budget: SERVER_INSTRUCTIONS stays <3900 (pinned by test; raised
# 3600 -> 3900 on 2026-11-04 for the shadow-memory HOST NOTE TOOLS block — deliberate raise #2,
# evidence: foss 5bf9e382 user report, memory routed to ephemeral host note tools).
#
# 2026-10-22 (DEV/RESEARCH/mcp-instructions-attachment-flow-20261022.md §3.5, P1): the
# ATTACHMENTS paragraph was inserted after VERBATIM (+238 served). Paid for by the doc's two
# named trims: the motivational tail (-81) and the weather worked-example sentence (-178).
# DEVIATION from the doc, documented: its "-80ish" estimate for the weather sentence under-
# counted (embedded span is 177 chars) and its arithmetic ("≈3,520") does not close — the
# two levers at estimated depth leave the premise OVER the 3600 pin, so per the doc's own
# fallback (the budget test re-pins the exact number) the example sentence was REMOVED whole,
# keeping the mechanism sentence ("It answers the one grounded thing you are missing so that
# YOU can do the rest"). 3599 - 81 - 178 + 238 = 3578, re-pinned by test.

SERVER_INSTRUCTIONS = """\
FaultLine is this user's memory. You do not have one.

Between conversations you forget everything. Inside a long one, your context is a scratchpad,
not a record — what you "remember" tens of thousands of tokens deep is often reconstruction.
FaultLine is not that. Everything it returns is something the user actually told you: captured,
validated, and stored as a real record you can point at. It persists across sessions, and across
models. It is the user's memory, held for you.

Use it to KNOW things about this user instead of guessing about them.

WHEN TO REACH FOR IT
- Before you answer anything that depends on who this user is — what they own, where they are,
  who they know, what they prefer, what they told you last week — call recall_memory FIRST.
- When you are about to say "I don't have access to that", "I don't know anything about you",
  "remind me…", or you are about to GUESS: that is the signal. Call recall_memory instead.
  Being about to guess is the trigger. It is checkable, and it is the one that matters.
- When the user states a fact — even in passing, even as an aside inside a question about
  something else — call remember_facts with their sentence verbatim: their own words, never
  your working text.

VERBATIM, ALWAYS — for the USER-memory tools only (recall_memory, remember_facts,
ingest_document, retract_fact — every tool that carries the user's words): pass the user's
own conversational turn BYTE-FOR-BYTE, the whole message, framing and all. Never paraphrase,
shorten, or drop the lead-in; the engine reads the raw sentence, and your rewrite is
information loss. Your OWN cortex notes are the opposite: write them yourself, in your own
words — never a paste of the user's message.

ATTACHMENTS: a file the user attached reaches you as extracted TEXT (<source> blocks), never bytes. Remember it with ingest_document using that text; ingest_file is only for callers that truly hold the raw bytes. Never fabricate base64.

- Fire NOTHING for chitchat — jokes, small talk, or anything you can answer from the
  conversation alone. Recall is for facts you lack, not for what you can just say.

WHAT IT IS NOT
It does not browse the web, fetch data, or run other tools. It answers the one grounded thing
you are missing so that YOU can do the rest.

HOST NOTE TOOLS ARE NOT THIS MEMORY
If the host offers built-in note tools (write_note, search_notes, view_note), those are temporary
notes — NOT FaultLine memory. When the user says "remember", "save to FaultLine", or asks you to
keep anything about them, route it through remember_facts / recall_memory only.

HOW TO READ WHAT IT RETURNS
Asserted lines are grounded truth — say them as your own knowledge, without hedging or citing a
lookup. Lines marked lower-confidence are tentative — say them tentatively. Never invent a fact
that recall did not return; an empty recall means you do not know, and saying so is correct.\
"""

# ── THE PATHWAY: the second memory, and the loop that keeps it alive ──────────────────────
#
# WHY THIS EXISTS (the owner's measured complaint, 2026-08-01)
# -----------------------------------------------------------
# "It's not so much a tool as it is me babysitting to get you to write things down; the
# alternative (proven) is increased token cost, repeat of errors, and a lack of understanding."
#
# The cortex READ lane works. The WRITE lane does not fire on its own. Production observation
# (2026-07-14): a real connected model called `cortex_recall` FIVE times in one session and
# `cortex_remember` ZERO times. Two fixes were already made and BOTH were necessary but neither was
# sufficient:
#   * the `cortex_remember` tool description was rewritten with triggers a conversational assistant can
#     recognise in itself ("being corrected IS the trigger") — good text, but a tool description
#     is read as an answer to "how do I call THIS", not to "does a loop like this exist";
#   * `cortex_usage_seed` standing rules were folded in — but they are gated behind
#     MEMORY_TEACHES/SAAS_MODE, so open-core deployments never see them at all.
#
# THE PART NOBODY FIXED, and it is a one-line diagnosis: THE PREMISE ITSELF SAYS THE OPPOSITE.
# The connect-time text above opens "FaultLine is this user's memory. You do not have one." That
# is the first and highest-signal thing a model reads about this product, it lands in the system
# prompt before any tool list is weighed, and it teaches a mental model with NO SLOT for the
# agent having a memory of its own. Every downstream nudge then has to argue against the premise
# we opened with. A model that has internalised "you do not have one" does not go looking for
# `cortex_remember`; it reads the pair as peripheral extras outside the stated purpose.
#
# So the pathway is taught HERE, in the same proactive channel, as an explicitly SECOND memory.
#
# SUBJECT-AGNOSTIC (checked line by line): no domain vocabulary, no persona. Deliberately NOT
# "a command that failed", "deploy", "test", "repo" — the developer framing that made a
# conversational assistant read the whole capability as "not for me". The triggers here are
# things a physician, a vet, a lawyer and a developer all experience in the same words: being
# corrected, being told it was wanted differently, something failing then working, finding a trap.
#
# MODEL-AGNOSTIC: it is prose in the memory's own doors. No hook, no client-side mechanism,
# nothing that lives outside the server. Claude, Mistral, a local Qwen and an OpenWebUI-hosted
# model all receive the identical bytes.
#
# OWN-WORDS WRITE LANE: the cortex bullet below teaches the agent to write the lesson ITSELF, in
# its own words — never a paste of the user's message. A verbatim mandate here would make pasted
# user content (transcripts, terminal output) the note body, and notes come back to the agent
# later: a prompt-injection vector removed by construction. Verbatim belongs to the user-memory
# lane and nowhere else.
#
# BUDGET: ~700 chars ≈ ~170 tokens, once per session, on the doors that carry server prose.

CORTEX_PATHWAY = """\
YOUR OWN NOTES — a second memory, kept separately
The memory above is theirs. FaultLine keeps a second one for YOU: not what is true about this
person, but how to work with them. Two calls, one loop.
- cortex_recall — before acting on something like a task you have done before, read your notes first.
  A task you have done before: cortex_recall is the FIRST call — before any recall_memory.
- cortex_remember — the moment you learn something, write the lesson down yourself, in your own words.
WRITE WHEN: they correct you; they say they wanted it done differently; something you tried failed
and a second attempt worked; you hit a trap worth a warning next time. Being corrected IS the
trigger — the clearest signal you get, and the one most often missed. What you never write down,
they must teach you again next session, and every session after.
These are working notes, not facts: hold them tentatively, and never repeat one back to the user
as something they told you.\
"""

# ⚠️ COMPACT variant for the TOOL-DESCRIPTION carrier — see the CARRIER PROBLEM note below.
# `instructions` and `info.description` are BOTH invisible to OpenWebUI; the per-operation tool
# description is the only proactive channel that reaches it. Same 1024-char shared budget as the
# tool's own text plus the standing-rule suffix, so this is deliberately terse and carries ONLY
# the two things a tool description cannot say on its own: that this is a LOOP with two halves,
# and what it costs the user when the write half never fires.
CORTEX_TOOL_PREMISE = """\
Your notes are a loop with two halves: cortex_recall before you act, cortex_remember the moment you learn. \
Being corrected is the write trigger. What you never write down, this user must teach you again \
next session. \
"""

# The cortex ENTRY-POINT pair. Each carries the compact loop premise as a PREFIX, for the same
# reason `recall_memory`/`remember_facts` do: a model reading either tool cold needs to know the
# OTHER half exists before it reads the mechanics of this one. A model that meets `cortex_recall` and
# never learns `cortex_remember` exists is exactly the production failure this is here to fix.
CORTEX_PATHWAY_TOOLS = ("cortex_recall", "cortex_remember")


def pathway_directions_enabled() -> bool:
    """Gate for the whole pathway layer. PURE env read — imports nothing.

    OFF ⇒ ``SERVER_INSTRUCTIONS`` and every tool description are BYTE-FOR-BYTE the pre-feature
    text on every door. Default ON: the capability this teaches is core-product, not SaaS, and a
    write lane that never fires is the defect being fixed. Rollback lever:
    ``CORTEX_PATHWAY_DIRECTIONS=false``.
    """
    return os.getenv("CORTEX_PATHWAY_DIRECTIONS", "true").strip().lower() in (
        "1", "true", "yes", "on")


def cortex_pathway_prefix(tool_name: str) -> str:
    """The compact pathway premise for `tool_name`, or "" if this tool does not carry it.

    Same shape as ``tool_premise_prefix`` and ``cortex_usage_seed.tool_description_suffix`` on
    purpose: one function, asked per tool, "" for the tools that do not want it. Applied in
    tools.py::TOOLS — the single source BOTH doors resolve from — so it reaches MCP-native and
    OpenAPI clients alike without either transport knowing it exists.
    """
    if not pathway_directions_enabled():
        return ""
    return CORTEX_TOOL_PREMISE if tool_name in CORTEX_PATHWAY_TOOLS else ""


# ── PAYING FOR THE PREFIX: an EXCISION, not a second copy of the prose ────────────────────
#
# THE 1024-CHAR CLIFF IS THE BINDING CONSTRAINT, and `cortex_remember` is the tool sitting
# closest to it. Measured 2026-08-14 (post own-words rewrite): 1004 with the pathway ON
# (prefix + post-excision text + standing-rule suffix), 1017 with the pathway OFF (no
# prefix, but the excision does not fire either, so the full WHEN span stays). Both
# configurations are pinned by test — do NOT trust free headroom in either; an over-limit
# description makes an OpenAI-compatible client reject THE ENTIRE TOOL LIST, not just this
# tool, so "let the serving seam clamp it" is not an option.
#
# The room is already there, spent twice: `cortex_remember`'s own "CALL THIS WHEN: the user corrects
# you ... Being corrected IS the trigger." is RESTATED almost verbatim by the standing rule
# ("When the user corrects you, redirects you to a different tool ... Being corrected IS the
# trigger; you will not get a clearer one"). Adding the prefix would make it three times.
#
# WHY AN EXCISION AND NOT A REWRITE. Rewriting the paragraph would leave TWO authored copies of
# this prose — one for flag-on, one for flag-off — and three drifting copies of tool descriptions
# is the exact defect that shipped a model the impoverished half of our own docs and cost this
# project real product quality. So the source keeps exactly ONE copy (tools.py::TOOLS) and the
# pathway layer REMOVES the span it supersedes. Consequences that make this the right shape:
#   * flag OFF is byte-for-byte the legacy description — nothing to keep in sync;
#   * if someone edits the source text, the excision simply does not match and NO-OPS (the full
#     text is served, budget guarded by the existing over-limit test) — it cannot silently serve
#     stale prose;
#   * there is no second place to forget to update.
#
# ANCHOR-BASED, not full-string (de-fragilised). The earlier shape stored the ENTIRE multi-line
# span to excise and required a byte-exact match — any edit to the surrounding prose silently
# no-oped. The anchors below are SHORT and STABLE: a start anchor ("CALL THIS WHEN:") and an end
# anchor (the sentence that carries the trigger). They survive edits to the prose between them,
# which is the robustness the excision needs to keep paying for the prefix as the description
# evolves.
#
# What survives the excision: the WHY ("so you stop repeating your own mistakes"), the firewall
# ("YOUR private working memory — NOT the user's fact"), and the hand-off to remember_facts. The
# WHEN it removes is carried by the prefix — which, unlike the standing suffix, is NOT
# MEMORY_TEACHES-gated, so open-core deployments keep a trigger they previously only had here.
_EXCISE_ANCHORS: dict[str, tuple[str, str]] = {
    "cortex_remember": ("CALL THIS WHEN:", "Being corrected IS the trigger."),
}


def apply_cortex_pathway(tool_name: str, description: str) -> str:
    """The tool-description half of the pathway layer: excise what the prefix supersedes, then
    prefix. Returns `description` UNCHANGED when the flag is off or the tool does not carry it.

    This is the ONE entry point tools.py calls, so the excision and the prefix can never be
    applied independently (excising without prefixing would delete a trigger and replace it with
    nothing — a silent regression).

    The excision is ANCHOR-BASED: it finds the start anchor, then the end anchor after it, and
    removes from the start anchor through the end of the sentence containing the end anchor
    (plus any trailing space, so the join stays clean). Both anchors are required; a missing
    start or end anchor is a clean no-op that serves the FULL text rather than a half-excised one.
    """
    if not pathway_directions_enabled():
        return description
    prefix = CORTEX_TOOL_PREMISE if tool_name in CORTEX_PATHWAY_TOOLS else ""
    if not prefix:
        return description
    anchors = _EXCISE_ANCHORS.get(tool_name)
    if anchors:
        start_anchor, end_anchor = anchors
        start_idx = description.find(start_anchor)
        if start_idx != -1:
            end_idx = description.find(end_anchor, start_idx)
            if end_idx != -1:
                # Excise from the start anchor through the end anchor (the anchors are short +
                # stable, so this survives edits to the prose between them). Consume trailing
                # whitespace so the join does not leave a double space.
                span_end = end_idx + len(end_anchor)
                while span_end < len(description) and description[span_end] == " ":
                    span_end += 1
                description = description[:start_idx] + description[span_end:]
    return prefix + description


# The premise as served. The pathway is APPENDED, never interleaved: the user-memory premise is
# load-bearing and pinned by test, and a model should finish learning what FaultLine is for the
# user before being handed a second memory of its own. Flag OFF ⇒ identical to the text above.
if pathway_directions_enabled():
    SERVER_INSTRUCTIONS = SERVER_INSTRUCTIONS + "\n\n" + CORTEX_PATHWAY

# ── THE CARRIER PROBLEM (validated against the spec + real client source, 2026-07-14) ─────
#
# `instructions` is the RIGHT slot — the MCP spec defines it on InitializeResult as text that
# "describes how to use the server and its features ... can be used by clients to improve the
# LLM's understanding of available capabilities", typically injected into the system prompt.
#
# But it is NOT A RELIABLE CARRIER on its own, for two independently-verified reasons:
#
#   1. MANY MCP CLIENTS SIMPLY DROP IT. The spec does not mandate what a client does with the
#      string. Claude Code injects it; there are open "please surface instructions" issues
#      against LangChain4j, litellm, 5ire and mcporter. A premise only sent via `instructions`
#      reaches an unknowable subset of clients.
#
#   2. THE OPENAPI DOOR NEVER SEES IT AT ALL — and that is the door OpenWebUI uses, which is
#      how our own users' models connect. OpenWebUI's `convert_openapi_to_tool_payload` reads
#      ONLY `paths` -> per-operation `operationId` / `description` / `summary` / `parameters`:
#
#          tool = {'name': operation.get('operationId'),
#                  'description': operation.get('description',
#                                 operation.get('summary', 'No description available.'))}
#
#      It does NOT read top-level `info.description`. So a premise parked there is dropped on
#      the floor by the exact client we built this for. (Verified against open-webui source, not
#      inferred from its docs — the docs do not say.)
#
# THEREFORE: the premise ALSO rides the per-operation TOOL DESCRIPTIONS, which every client on
# every transport reads by construction — it is the payload they exist to deliver. `instructions`
# and `info.description` stay as well: they are free where supported, and belt-and-braces is the
# correct posture when the carrier is client-defined.
#
# Cost control: the FULL premise on all 8 tools would be paid 8x for one idea. The COMPACT
# premise below goes on the two ENTRY-POINT tools only (recall_memory, remember_facts) — the two
# that constitute the whole product loop, and the only two a model must learn to reach for
# unprompted. The other six are reached once the model already understands what this is.

# ⚠️ HARD BUDGET. OpenAI-compatible function-calling REJECTS a tool `description` over
# OPENAI_DESCRIPTION_LIMIT (1024) chars. The MCP/Anthropic door has no such cap, so the long
# canonical text was fine there — but the moment both doors were unified onto it AND this premise
# was prefixed, three tools blew past 1024 and the OpenWebUI-hosted model's ENTIRE TOOL LIST was
# rejected. FaultLine went from "the model doesn't know when to call us" to "the model cannot call
# us at all". Shipped and caught in production the same day.
#
# So this is deliberately TERSE. Every char here is charged against the same 1024 budget as the
# tool's own text, on the tightest door. Mechanics belong in the seeded rules (relevance-gated,
# free when irrelevant); this carries ONLY: what it is, why it beats your own recall, and the one
# checkable trigger.
TOOL_PREMISE = """\
FaultLine is this user's memory — you have none. What it returns is what they actually told you, \
not your own recollection. Use it to KNOW, not guess: if you are about to guess, to say you lack \
access, or to ask them to remind you — call it. \
"""


# ── The same premise for OpenAPI consumers ────────────────────────────────────────────────
#
# OpenWebUI (and any OpenAPI tool-caller) reads `info.description` off the tool server. It is
# the ONLY server-level prose an OpenAPI client ever sees — there is no `instructions` field in
# OpenAPI, so this slot carries the whole premise. It was empty: the app was constructed as
# FastAPI(title="FaultLine MCP", version="1.0.0") with no description at all.
#
# Kept identical to the MCP text on purpose. A model must not learn a different FaultLine
# depending on which door it came through — that is the transport-parity rule, applied to
# meaning rather than to code.

OPENAPI_DESCRIPTION = SERVER_INSTRUCTIONS

# ── /v1 ───────────────────────────────────────────────────────────────────────────────────
#
# /v1 has TWO audiences: a human integrator reading the docs, and an agent consuming the
# OpenAPI. The existing one-liner serves the human and says nothing to the agent. Lead with the
# human framing (it is the SDK's front page), then hand the agent the same premise as every
# other door, so an agent that arrives via /v1 learns the same FaultLine.

_V1_HUMAN_BLURB = (
    "Per-tenant, write-validated knowledge-graph memory. Remember, recall, correct, and learn "
    "— the same validated engine an MCP client uses."
)

V1_DESCRIPTION = _V1_HUMAN_BLURB + "\n\n---\n\n" + SERVER_INSTRUCTIONS


# The ENTRY-POINT tools: the two a model must learn to reach for on its own. They carry the
# compact premise as a PREFIX (before the tool's own how/when text), because a model reading a
# tool spec cold needs the "why would I ever call this" before the "how do I call it".
PREMISE_TOOLS = ("recall_memory", "remember_facts")


def tool_premise_prefix(tool_name: str) -> str:
    """The compact premise for `tool_name`, or "" if this tool does not carry it.

    Mirrors `cortex_usage_seed.tool_description_suffix` in shape on purpose: one function, asked
    per tool, returning "" for the tools that do not want it. Applied in tools.py::TOOLS, which
    is the single source both doors resolve from — so this reaches MCP and OpenAPI clients alike
    without either transport knowing it exists.
    """
    return TOOL_PREMISE if tool_name in PREMISE_TOOLS else ""


__all__ = [
    "SERVER_INSTRUCTIONS", "OPENAPI_DESCRIPTION", "V1_DESCRIPTION",
    "TOOL_PREMISE", "PREMISE_TOOLS", "tool_premise_prefix",
    "CORTEX_PATHWAY", "CORTEX_TOOL_PREMISE", "CORTEX_PATHWAY_TOOLS",
    "_EXCISE_ANCHORS", "cortex_pathway_prefix", "apply_cortex_pathway",
    "pathway_directions_enabled",
    "SUPPORTED_PROTOCOL_VERSIONS", "LATEST_PROTOCOL_VERSION",
    "negotiate_protocol_version", "discover_result",
    "decode_header_value", "ERROR_HEADER_MISMATCH", "ERROR_MISSING_REQUIRED_CLIENT_CAPABILITY",
    "ERROR_UNSUPPORTED_PROTOCOL_VERSION", "_LIST_TTL_MS", "_LIST_CACHE_SCOPE",
]
