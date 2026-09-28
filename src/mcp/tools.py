"""MCP tool definitions and schemas for FaultLine endpoints."""

TOOLS = [
    {
        "name": "recall_memory",
        # BUDGET: premise + this + the standing-rule suffix must stay under 1024 chars (the
        # OpenAI function-description limit). Trimmed from 1025 -> ~560: the /expand examples and
        # the prose-style coaching moved out. Keep it tight; the seeded rules carry mechanics.
        #
        # AUTHORSHIP (2026-08-14 incident class): the pollution that actually reaches this lane
        # is OPERATIONAL history — a build agent's own commits/builds/logs/rate-limit prose. That
        # class is NOT user memory. The query itself is authorship-scoped: a HUMAN's question,
        # verbatim; the agent's own working queries do not belong here.
        "description": "Call when the user (a HUMAN) asks you about themselves, their people, "
                       "or their world; pass THEIR message VERBATIM as `query` — never your own "
                       "working query, never a keyword. Not inert: text may be captured and "
                       "corrections acted on. Save with remember_facts. Treat results as your own "
                       "knowledge — speak to the user as 'you'; less-certain lines are "
                       "unconfirmed — say them tentatively. Do NOT call for chitchat, jokes, or "
                       "greetings. Operational history — commits, builds, deploys, logs, rate "
                       "limits, your own past work sessions — is NOT user memory. "
                       # CROSS-REFERENCE, not a copy: the full /expand docs + examples live on
                       # learn_facts, which has the headroom. Per-tool 1024 budgets are spent
                       # unevenly — chain to the tool with room instead of paying twice.
                       "To map a topic's concepts, see /expand on learn_facts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's current message copied VERBATIM and in full — do NOT summarize, shorten, or reduce it to a keyword or topic. Keep every word, especially 'not', 'no', 'now', 'actually', 'instead' and any names/values. The backend extracts the search topic AND decides intent (recall vs correction) from the whole sentence itself. Authorship-scoped: this is a HUMAN's question to you; your own working queries (operational history — commits, builds, logs, past sessions) do not belong here. Required — never leave empty."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "remember_facts",
        # BUDGET: see recall_memory. Trimmed 683 -> ~480.
        #
        # ── THE AUTHORSHIP LINE (production incident 2026-08-14) ──────────────────────────
        # A build agent connected over MCP passed its OWN operating brief (engineering prose
        # about brain-rate governance) into remember_facts AND recall_memory, VERBATIM —
        # obeying the old contract ("pass the user's message VERBATIM"), which never
        # separated a HUMAN's conversational turn from the AGENT's own working text. Result
        # on a real user's memory: 21 durable Class-A facts at confidence 1.0 with junk entities
        # ("cerebras free tier" typed Object, "50" as an entity, "wedged_failopen"), plus 7
        # engine-grown rel_types (log/permit/evidence_by/see/allow/hit/real_tier_key) minted
        # from the junk.
        #
        # THE FIX IS AUTHORSHIP, NOT CONTENT. The write lane is defined by WHO authored the
        # text: remember_facts accepts ONLY a human being's own conversational turn, verbatim
        # — the agent's own working text NEVER qualifies, no matter what it looks like.
        # Content-based heuristics ("looks technical", "mentions deploys") are explicitly
        # FORBIDDEN by the owner: they would also filter genuine human turns and weaken
        # capture. What the AGENT learns while working (command results, fixes, procedures,
        # commits) is not user memory. Verbatim passing stays EXACTLY as strong as before for
        # genuine human turns — this scoping must not weaken it.
        "description": "Save what a HUMAN being said to you in conversation — typed or "
                       "spoken, about themselves, another person, or their world — even in "
                       "passing, and when they correct a prior fact. Do not ask permission — "
                       "default to calling it; skip only pure questions or chitchat. Pass "
                       "their words VERBATIM as `text`, every word, asides and all: do NOT "
                       "extract, summarize, or restructure — the engine needs the raw "
                       "sentence. NEVER pass your own working text — brief, plan, analysis, "
                       "or tool output — as the user's message. A correction supersedes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    # AUTHORSHIP-SCOPED (2026-08-14 incident — see the comment block above):
                    # the raw material of the user lane is a HUMAN's conversational turn and
                    # nothing else. The agent's own working text never qualifies.
                    "description": "The user's — a HUMAN being's — own conversational turn, "
                                   "copied VERBATIM and in full: the raw sentence(s) exactly "
                                   "as they said them, every word including interjections and "
                                   "asides ('oh by the way'). Do NOT pre-extract, summarize, "
                                   "or restructure it into facts, triples, or line items — "
                                   "FaultLine's engine does ALL extraction, typing, and "
                                   "structuring; it needs the raw words. NEVER pass your own "
                                   "working text (task briefs, plans, analyses, summaries, "
                                   "tool output) — even one a human indirectly originated, it "
                                   "is not their conversational turn. Required — never leave "
                                   "empty."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "ingest_document",
        "description": "Store a document, article, PDF text, or long-form content in memory. "
                       "Use when the user shares or pastes a document, article, notes, or any "
                        "multi-paragraph body of text and wants it remembered — the whole text is "
                        "chunked, retained verbatim, and mined for facts automatically. Copy the "
                        "user's ENTIRE message verbatim as `text` — including any framing sentence "
                        "('Here's an article I want to keep:') and the title. Do NOT strip the "
                        "lead-in, summarize, or pre-extract facts. "
                       "Provide source_ref (URL/filename) or title when known so extracted facts "
                       "carry a citation. Processing runs in the BACKGROUND: this returns "
                       "immediately with status='pending' and an eta_seconds estimate, and the "
                       "document's facts become searchable shortly after — tell the user it's "
                       "being processed and to ask again in a moment. Not for conversational "
                       "messages — use remember_facts for those. "
                       # AUTHORSHIP SCOPING (2026-08-14 incident — see the remember_facts
                       # comment): the document lane takes what a HUMAN shared, never the
                       # agent's own working text. This lane had the most headroom (~859
                       # served), but only ~35 chars of it — the sentence is deliberately
                       # the compact form.
                       "A user document is one a HUMAN shared — never your own working "
                       "text, plans, or session output. May supersede.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The user's ENTIRE message copied VERBATIM, in full — including "
                                   "any framing or lead-in sentence (e.g. 'Here's an article I want "
                                   "to keep:'), not just the article body. Do NOT strip, summarize, "
                                   "shorten, or pre-extract facts from it. FaultLine chunks and "
                                   "extracts everything itself; it needs the raw message. A user "
                                   "document is one a HUMAN shared — never your own working text "
                                   "or session output. Required — never leave empty."
                },
                "source_ref": {
                    "type": "string",
                    "description": "Optional: where this document came from — a URL, filename, or "
                                   "citation string. Stored with every fact extracted from the "
                                   "document so recall can cite its source."
                },
                "title": {
                    "type": "string",
                    "description": "Optional: the document's title. Used as the source reference "
                                   "when source_ref is not provided."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "retry_document",
        # ANNOTATED FROM WHAT THE HANDLER REACHES (the rule every other entry here follows):
        # this re-queues a failed document's unread chunks through the SAME background drain
        # ingest_document uses, whose /ingest writes post at the HIGHEST provenance authority
        # (source="mcp" -> user_stated) and can therefore supersede/archive an immutable-slot
        # fact exactly like ingest_document can. Not a delete; not purely additive either.
        # idempotentHint is NOT claimed: double-fire is safe (the flip is status-guarded) but
        # a genuine re-extraction bumps confirmed_count like any re-ingest.
        "description": "Retry the unread sections of a document that finished importing only "
                       "partially (status 'partial'/'error' — some sections could not be read, "
                       "usually a transient model failure). The original text is retained, so "
                       "only the failed sections are reprocessed. Use when a document's import "
                       "report or document_status shows failed sections, or the user asks to "
                       "re-try/re-import a document that did not fully land. Omit document_id "
                       "to retry every retriable document matching source_ref. Safe to call "
                       "twice: an already-queued document is reported as already active. "
                       "Like ingest_document, re-ingested facts may SUPERSEDE (archive) "
                       "conflicting facts already in memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "The document id to retry (from document_status or an import "
                                   "report). Optional if source_ref is given."
                },
                "source_ref": {
                    "type": "string",
                    "description": "Alternative to document_id: retry every partially-imported "
                                   "document with this URL/filename."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": []
        }
    },
    {
        "name": "document_status",
        "description": "Report the import status of previously ingested documents: which are "
                       "still processing, which finished only PARTIALLY (sections that could "
                       "not be read, with the reason per failed section), and which are "
                       "retriable via retry_document. Use when the user asks whether a document "
                       "finished importing, what was missed, or what to re-try.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "Optional: report one document only. Omit for all."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": []
        }
    },
    {
        "name": "review_structure",
        # THE SHELF, NOT THE MEMORIES ON IT. This tool reads and edits the engine's own
        # scaffolding (cue classes, engine-minted rel_types, engine groupings, the type
        # ladder). It cannot read, write, or delete a single stored fact about the user —
        # THE HARD LINE, enforced at the backend endpoints, not just described here.
        "description": "See the STRUCTURE FaultLine's engine built for itself from this "
                       "user's turns — word-classes it derived (e.g. it decided "
                       "'colleague' means a person you KNOW), relation types it minted, "
                       "groupings it formed, and type ladders it laid down — and switch "
                       "off any of it that is wrong. This is the engine's own filing "
                       "system, NOT the user's facts; nothing here can change or delete a "
                       "memory. Call it with no arguments when the user says the system "
                       "MISREAD a word or wired something up wrong ('a colleague isn't "
                       "something I own'), or asks what it has figured out about how they "
                       "talk. Then call it again with the exact `cue` and `category` from "
                       "the list to retire that one — reversible with `reactivate`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Optional filter: cues | rel_types | groupings | ladder. Omit to see everything."
                },
                "cue": {
                    "type": "string",
                    "description": "To RETIRE one derived word-class member: its exact `cue` as listed by a previous no-argument call. Requires `category` too. Never guess this — read it first."
                },
                "category": {
                    "type": "string",
                    "description": "The exact `category` of the cue being retired, as listed. Required alongside `cue`."
                },
                "reactivate": {
                    "type": "boolean",
                    "description": "Put a previously retired cue back (undo). Default false."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": []
        }
    },
    {
        "name": "ingest_file",
        # ANNOTATED FROM WHAT THE HANDLER REACHES: it runs the FULL ingest_document lane on
        # any extracted text (which can supersede at its tier) and persists the file bytes
        # into the per-tenant artefacts table. A write, like its siblings.
        "description": "Upload a FILE (PDF or image) a HUMAN shared, as base64 in "
                       "`data_b64`, and FaultLine retains it, extracts its text "
                       "deterministically (no model reads the file), and mines it exactly "
                       "like ingest_document — embedded figures are bound to the text "
                       "around them by page geometry. Use when the user shares an image or "
                       "a PDF/file attachment. A file with no readable text (e.g. a photo) "
                       "is still RETAINED and reported honestly. May supersede existing "
                       "facts, like any ingest. Larger files: the REST /ingest_file door.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data_b64": {
                    "type": "string",
                    "description": "The file's RAW BYTES as base64 — the whole file, not "
                                   "an excerpt. Required."
                },
                "filename": {
                    "type": "string",
                    "description": "Optional: the file's name (used as a citation label)."
                },
                "source_ref": {
                    "type": "string",
                    "description": "Optional: where the file came from (URL/path), for citation."
                },
                "title": {
                    "type": "string",
                    "description": "Optional: document title."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": ["data_b64"]
        }
    },
    {
        "name": "learn_facts",
        # /expand DOCUMENTATION LIVES HERE — deliberately.
        #
    # The 1024-char OpenAI limit is a PER-TOOL budget, not a global one, and the budget is
    # unevenly spent: recall_memory sits at ~1018 (6 spare) while this tool sat at 366 (658
    # spare). /expand is intercepted by a SHARED handler (server.py `_expand_command`) used by
        # BOTH recall_memory_tool and learn_facts_tool, so it is equally at home on either — and
        # this is its semantic home anyway ("learn/expand a topic").
        #
        # So: the FULL /expand docs + worked examples go on the tool with headroom, and the
        # crowded tool carries a one-line CROSS-REFERENCE. The model reads every tool description,
        # so a pointer is enough — this is how you chain past a per-tool cap without paying for
        # the same text twice.
        "description": (
            "Two uses. (1) /expand — build a concept map for a topic, so facts the user later "
            "shares about that domain have somewhere to file. Pass as `text`:\n"
            "  /expand networking\n"
            "  /expand networking online\n"
            "  /expand networking online https://example.com/networking-guide\n"
            "'online' lets it source from the web; a URL seeds it from that page. /expand maps how "
            "concepts RELATE — it does not make you an expert on the topic. Use it BEFORE bulk "
            "ingest so the ontology exists to file facts against.\n"
            "(2) Ontological statements you generate — 'X is a subclass of Y', 'X is an instance "
            "of Y', 'X is a part of Y', one per line. This maps how concepts relate, not general "
            "knowledge. Stored as Class B (staged), confirmed over time. May supersede an existing learned fact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Your generated ontological statements — one per line, "
                                   "using 'X is a subclass of Y', 'X is an instance of Y', "
                                   "or 'X is a part of Y' forms only"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "retract_fact",
        "description": "THE DEFAULT tool for a plain-language request to forget, delete, erase, or "
                       "remove a memory — 'forget that I have a dog', 'delete my email address', "
                       "'remove what you know about X'. Do NOT decompose it yourself; pass a HUMAN's "
                       "words VERBATIM as `text` (their own request — never your working text) and "
                       "the backend extracts which fact(s) to retract. "
                       "For a CORRECTION (the user giving a NEW value for something), use "
                       "remember_facts instead, NOT this. Use forget_fact ONLY when the user pins one "
                       "specific already-stored fact by its exact stored value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The statement to retract (e.g., 'forget that X is Y', 'X is not Z')"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "forget_fact",
        "description": "Use ONLY for a precise tombstone of ONE already-stored fact about a NAMED "
                       "target, when the user points at a specific stored value you can pin exactly "
                       "— e.g. 'forget that Ada is my spouse', 'delete that my work email is "
                       "alex@example.com'. You MUST fill 'subject' (who the fact is about; 'me' for the "
                       "user) and 'old_value' (the stored value to remove); 'rel_type' narrows it "
                       "further. A subject alone, or a relation alone, is refused. "
                       "Category words are NOT stored values ('dog', 'email address', "
                       "'job' name a kind of fact, not one fact) — a plain-language forget like "
                       "'forget that I have a dog' goes to retract_fact. NEVER for a broad/bulk "
                       "request ('forget everything', 'wipe my memory') — there is no bulk forget. "
                       "For a CORRECTION (a NEW value) use remember_facts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "WHOSE fact to forget — 'me' for the user, or the named person/thing. "
                                   "Required: a forget MUST name exactly one target, never a bulk wipe."
                },
                "rel_type": {
                    "type": "string",
                    "description": "Optional: the relationship of the specific fact (e.g. occupation, "
                                   "has_pet, has_email) to narrow the forget to one fact."
                },
                "old_value": {
                    "type": "string",
                    "description": "REQUIRED: the specific value/object of the fact to remove (e.g. "
                                   "the email address, the pet's name). Without it a subject or a "
                                   "relation alone would sweep every matching fact, so the call is "
                                   "refused."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated user automatically (header-injected, or FAULTLINE_USER_ID), and a value that does not match the bound identity is rejected. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            # `old_value` IS required and the runtime refuses without it on all three doors.
            # Advertising it as optional made the MACHINE-READABLE half of this tool's contract
            # false: a schema-compliant call `{"subject": "me"}` is answered with HTTP 400.
            "required": ["subject", "old_value"]
        }
    }
]

# ── CANONICAL ORDER (2026-07-28 spec §tools): deterministic tool order is what enables client
# caching and stable prompt-cache hit rates — and ORDER IS DISCOVERABILITY for the models that
# read this list. The user-memory pair leads (the premise carriers), then the document/
# structure/learn/retract lanes. Deterministic and declarative: reorder the tuple here, not by
# hand-moving dict blocks. Non-breaking — no client depends on list position, and `annotations`/`title`/
# `outputSchema`/premise/standing-rule application is name-keyed, so it is order-independent.
_CANONICAL_ORDER = (
    "recall_memory",      # user-memory READ entry point (premise carrier)
    "remember_facts",     # user-memory WRITE entry point (premise carrier)
    "ingest_document",    # document / long-form lane
    "ingest_file",        # binary door: PDF/image via base64 (same document lane + artefact retention)
    "document_status",    # document-lane STATUS (pure read; caller-relayable)
    "review_structure",   # ENGINE-STRUCTURE read + cue retirement (the shelf, never the memory)
    "retry_document",     # document-lane CHUNK-LEVEL RETRY (re-runs the ingest lane)
    "learn_facts",        # ontological / /expand lane
    "retract_fact",       # plain-language forget
    "forget_fact",        # precise single-fact tombstone
)
if tuple(t["name"] for t in TOOLS) != _CANONICAL_ORDER:
    _by_name = {t["name"]: t for t in TOOLS}
    assert set(_by_name) == set(_CANONICAL_ORDER), (
        "TOOLS drifted from _CANONICAL_ORDER — add the new tool to the tuple"
    )
    TOOLS = [_by_name[n] for n in _CANONICAL_ORDER]
    del _by_name


# MCP SPEC COMPLIANCE: annotations (P1), titles (P9), outputSchema (P4).
# Non-breaking: clients that understand these fields use them; others ignore them.
# Evidence: https://modelcontextprotocol.io/specification/2025-06-18/server/tools
_ANNOTATIONS = {
    # ⚠️ NEITHER READ TOOL IS READ-ONLY, AND BOTH USED TO SAY THEY WERE.
    #
    # recall_memory looks like a pure read and is not. The route is the BRAIN's, not the model's:
    # a turn classified RETRACTION/CORRECTION returns retract_fact_tool (a destructive
    # supersede), a STATEMENT calls remember_facts_tool as a non-eating fallback, and the
    # intent-independent harvest ingests fact-bearing spans on EVERY route. The only thing in
    # front of the destructive divert is ungated on open-core. Demonstrated live, not inferred: a single recall_memory call
    # carrying "I do not have a pet X any more" wrote a fact, and forget_fact then reported
    # hard_delete on it.
    #
    # These hints are how a host decides what may run WITHOUT asking the user first, so claiming
    # read-only for a tool that can delete is the one direction of this lie that costs something.
    # The behaviour is deliberate and owner-ruled ("brain-not-transport"); the ANNOTATION was
    # simply wrong about it, and the annotation is the cheap half to correct.
    "recall_memory": {"readOnlyHint": False, "destructiveHint": True},
    "retract_fact": {"readOnlyHint": False, "destructiveHint": True},
    "forget_fact": {"readOnlyHint": False, "destructiveHint": True},
    # review_structure: NOT read-only (a `cue`+`category` call flips is_active), but NOT
    # destructive either, and that pair is deliberate rather than lazy. It is the only write
    # tool on this server that cannot reach a memory row at all: the backend endpoint touches
    # `linguistic_cues` and nothing else, no row is deleted (only a boolean flips), and
    # `reactivate` restores the prior state exactly. Verified against the endpoint, not the
    # tool name — which is the depth the annotations above were corrected to.
    "review_structure": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    # The WRITE tools were unannotated, which is not neutral: the spec's default for an
    # un-annotated tool is destructive, so an automated caller had to assume all four could
    # destroy something and refuse to parallelize or retry them.
    #
    # ⚠️ remember_facts IS destructive, and the obvious annotation is WRONG. It looks purely
    # additive from its name and its docstring, but the route is the BRAIN's, not the model's:
    # a turn the classifier calls RETRACTION or CORRECTION is handed straight to
    # retract_fact_tool → /retract/correct, which supersedes and deletes
    # (``server.py``, ``_remember_facts_tool_impl``: ``if intent in ("RETRACTION", "CORRECTION"):
    # return await retract_fact_tool(...)``). So "I do not have a dog any more" reaches
    # remember_facts and DELETES. destructiveHint stays TRUE for that reason; verified against
    # the divert site, not inferred from the tool's name.
    #
    # idempotentHint is deliberately NOT claimed anywhere here: a repeated ingest bumps
    # confirmed_count, which can advance a Class-B staged row's promotion by one confirmation,
    # so a retry is not a no-op even though the natural-key upsert absorbs the row itself.
    # ⚠️ EVERY WRITE TOOL ON THIS SERVER CAN ARCHIVE EXISTING STATE. Three of these were
    # annotated "additive only" on the strength of reading the MCP handlers end to end — which
    # was the wrong depth. The handler is not where the destruction happens; the backend it
    # POSTs to is:
    #   • ingest_document posts source="mcp" -> fact_provenance user_stated (the HIGHEST
    #     authority rank). On a rel_type whose temporal_class is "immutable", the same-slot
    #     temporal pass calls _soft_supersede_conflicting_fact, which sets superseded_at on the
    #     conflicting row. Equal-or-higher authority supersedes. So a document that names a
    #     different born_in / born_on / nationality / pref_name / has_gender / also_known_as /
    #     duration ARCHIVES the fact already there.
    #   • learn_facts posts source="llm_learn" -> llm_learned, a LOWER rank, so the trust
    #     firewall drops it against a user-stated fact — but against an equal-rank learned fact
    #     on an immutable rel_type it supersedes exactly the same way.
    # None of that is a bug — it is the supersession model working. It simply is not "only
    # additive updates", which is what destructiveHint False tells a client.
    #
    # idempotentHint is claimed nowhere: a repeated ingest bumps confirmed_count, which can
    # advance a Class-B staged row's promotion by one confirmation.
    "remember_facts": {"readOnlyHint": False, "destructiveHint": True},
    "ingest_document": {"readOnlyHint": False, "destructiveHint": True},
    "learn_facts": {"readOnlyHint": False, "destructiveHint": True},
    # retry_document re-runs the ingest lane (source="mcp" -> user_stated, the highest
    # authority): same archive-on-immutable-slot class as ingest_document. Verified against
    # what its handler reaches (the drain -> /ingest), not its name.
    "retry_document": {"readOnlyHint": False, "destructiveHint": True},
    # ingest_file runs the same ingest lane on extracted text AND persists the bytes —
    # a write through and through.
    "ingest_file": {"readOnlyHint": False, "destructiveHint": True},
    # document_status is a pure read of the documents registry — no divert, no write lane,
    # no engagement counter. The server's first honestly-read-only tool.
    "document_status": {"readOnlyHint": True, "destructiveHint": False},
}
_TITLES = {
    "recall_memory": "Recall Memory",
    "remember_facts": "Remember Facts",
    "ingest_document": "Ingest Document",
    "ingest_file": "Ingest File (PDF/Image)",
    "document_status": "Document Import Status",
    "review_structure": "Review Engine Structure",
    "retry_document": "Retry Failed Document Sections",
    "learn_facts": "Learn / Expand",
    "retract_fact": "Retract Fact",
    "forget_fact": "Forget Fact",
}
_OUTPUT_SCHEMAS = {
    "recall_memory": {
        "type": "object",
        "properties": {
            "memory": {"type": "string", "description": "The recalled memory as natural-language prose."},
            "status": {"type": "string", "description": "Recall outcome (ok, no_ingest, error, or any diverted learn/retract/ingest status). Open set — recall_memory_tool delegates and returns the full delegated surface."},
        },
    },
    "remember_facts": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Write outcome (stored, no_ingest, query_detected, rejected, ingest_disabled, corrected, …). Open set: routed CORRECTION/RETRACTION returns the retract_fact status."},
            "committed": {"type": "integer", "description": "Number of facts committed to long-term storage."},
            "message": {"type": "string"},
        },
    },
    "ingest_document": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Ingest outcome (pending, accepted, rejected)."},
            "eta_seconds": {"type": "integer", "description": "Estimated seconds until the document's facts become searchable."},
            "message": {"type": "string"},
        },
    },
    "ingest_file": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "retained | retained_no_text | the document lane's status | rejected | invalid_request."},
            "media_type": {"type": "string"},
            "message": {"type": "string", "description": "The honest outcome sentence (what was kept, what entered memory)."},
        },
    },
    "document_status": {
        "type": "object",
        "properties": {
            "documents": {
                "type": "array",
                "description": "One entry per document: id, title, status, chunk_count, "
                               "chunks_failed, failed_chunks (chunk + reason per unread "
                               "section), facts_committed, retriable.",
                "items": {"type": "object"},
            },
        },
    },
    "review_structure": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "ok | error (a retire/reactivate call returns the backend's own status)."},
            "structure": {"type": "object", "description": "Engine-grown structure by kind: cues, rel_types, groupings, ladder."},
            "counts": {"type": "object", "description": "Row count per kind."},
            "message": {"type": "string"},
        },
    },
    "retry_document": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "retry_queued | already_active | nothing_to_retry | invalid_request | soft_error."},
            "retried": {"type": "integer", "description": "Documents re-queued."},
            "eta_seconds": {"type": "integer", "description": "Estimated seconds until the re-queued sections are searchable."},
            "message": {"type": "string"},
        },
    },
    "learn_facts": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Learn outcome (stored, staged, no_ingest)."},
            "committed": {"type": "integer", "description": "Number of ontological statements stored."},
            "message": {"type": "string"},
        },
    },
    "retract_fact": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Retraction outcome (corrected, retracted, clarification_needed, no_ingest)."},
            "retracted": {"type": "boolean"},
            "message": {"type": "string"},
        },
    },
    "forget_fact": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Forget outcome (forgotten, retracted, no_ingest)."},
            "message": {"type": "string"},
        },
    },
}
for _t in TOOLS:
    _name = _t.get("name", "")
    if _name in _ANNOTATIONS:
        _t["annotations"] = _ANNOTATIONS[_name]
    if _name in _TITLES:
        _t["title"] = _TITLES[_name]
    if _name in _OUTPUT_SCHEMAS:
        _t["outputSchema"] = _OUTPUT_SCHEMAS[_name]


def validate_text(text: str) -> str | None:
    """Return error message if text is invalid, None if valid."""
    if not isinstance(text, str):
        return "text must be a string"
    if len(text.strip()) == 0:
        return "text must not be empty"
    return None


def validate_user_id(user_id: str) -> str | None:
    """Return error message if user_id is invalid, None if valid."""
    if not isinstance(user_id, str):
        return "user_id must be a string"
    if len(user_id.strip()) == 0:
        return "user_id must not be empty"
    return None



def validate_query(query: str) -> str | None:
    """Return error message if query is invalid, None if valid."""
    if not isinstance(query, str):
        return "query must be a string"
    if len(query.strip()) == 0:
        return "query must not be empty"
    return None
