"""MCP tool definitions and schemas for FaultLine endpoints."""

TOOLS = [
    {
        "name": "recall_memory",
        # BUDGET: premise + this + the standing-rule suffix must stay under 1024 chars (the
        # OpenAI function-description limit). Trimmed from 1025 -> ~560: the /expand examples and
        # the prose-style coaching moved out. Keep it tight; the seeded rules carry mechanics.
        #
        # AUTHORSHIP + LANE ROUTING (2026-08-14 incident class): the pollution that actually
        # reaches this lane is OPERATIONAL history — a build agent's own commits/builds/logs/
        # rate-limit prose. That class is NOT user memory and must be routed to cortex_recall
        # BY NAME, not merely excluded as "a task". The query itself is authorship-scoped too:
        # a HUMAN's question, verbatim; the agent's own working queries belong to cortex_recall.
        "description": "Call when the user (a HUMAN) asks you about themselves, their people, "
                       "or their world; pass THEIR message VERBATIM as `query` — never your own "
                       "working query, never a keyword. Not inert: text may be captured and "
                       "corrections acted on. Save with remember_facts. Treat results as your own "
                       "knowledge — speak to the user as 'you'; less-certain lines are "
                       "unconfirmed — say them tentatively. Do NOT call for chitchat, jokes, or "
                       "greetings. Operational history — commits, builds, deploys, logs, rate "
                       "limits, your own past work sessions — is NOT user memory: use "
                       "cortex_recall. "
                       # CROSS-REFERENCE, not a copy: the full /expand docs + examples live on
                       # learn_facts, which has the headroom. Per-tool 1024 budgets are spent
                       # unevenly — chain to the tool with room instead of paying twice.
                       "To map a topic's concepts, see /expand on learn_facts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's current message copied VERBATIM and in full — do NOT summarize, shorten, or reduce it to a keyword or topic. Keep every word, especially 'not', 'no', 'now', 'actually', 'instead' and any names/values. The backend extracts the search topic AND decides intent (recall vs correction) from the whole sentence itself. Authorship-scoped: this is a HUMAN's question to you; your own working queries (operational history — commits, builds, logs, past sessions) belong to cortex_recall instead. Required — never leave empty."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
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
        # on the owner's seat: 21 durable Class-A facts at confidence 1.0 with junk entities
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
        # commits) has its own lane: cortex_remember. Verbatim passing stays EXACTLY as
        # strong as before for genuine human turns — this scoping must not weaken it.
        "description": "Save what a HUMAN being said to you in conversation — typed or "
                       "spoken, about themselves, another person, or their world — even in "
                       "passing, and when they correct a prior fact. Do not ask permission — "
                       "default to calling it; skip only pure questions or chitchat. Pass "
                       "their words VERBATIM as `text`, every word, asides and all: do NOT "
                       "extract, summarize, or restructure — the engine needs the raw "
                       "sentence. NEVER pass your own working text — brief, plan, analysis, "
                       "or tool output — as the user's message; what YOU learn while working "
                       "goes to cortex_remember. A correction supersedes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    # AUTHORSHIP-SCOPED (2026-08-14 incident — see the comment block above):
                    # the raw material of the user lane is a HUMAN's conversational turn and
                    # nothing else. The agent's own working text belongs to cortex_remember.
                    "description": "The user's — a HUMAN being's — own conversational turn, "
                                   "copied VERBATIM and in full: the raw sentence(s) exactly "
                                   "as they said them, every word including interjections and "
                                   "asides ('oh by the way'). Do NOT pre-extract, summarize, "
                                   "or restructure it into facts, triples, or line items — "
                                   "FaultLine's engine does ALL extraction, typing, and "
                                   "structuring; it needs the raw words. NEVER pass your own "
                                   "working text (task briefs, plans, analyses, summaries, "
                                   "tool output) — even one a human indirectly originated, it "
                                   "is not their conversational turn; what YOU learn while "
                                   "working goes to cortex_remember. Required — never leave "
                                   "empty."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "ingest_document",
        # ATTACHMENT FLOW FIRST (2026-10-22 rewrite, evidence: DEV/RESEARCH/
        # mcp-instructions-attachment-flow-20261022.md §3.1). OWUI Tool Search (experimental)
        # truncates a deferred tool's description to its FIRST 100 chars, so the attachment
        # routing must LEAD. Served length 1008/1024 — measured incl. suffix passes (the
        # document lane carries an EMPTY standing-rule suffix, so this string IS the budget).
        "description": (
        "Store a document a HUMAN shared: pasted text, an article, notes, or an ATTACHED FILE. "
        "Attachments: the host (e.g. OpenWebUI) has already extracted the file's text into "
        "<source> blocks in this conversation - pass THAT extracted text as `text`. It IS the "
        "file; do not re-extract, never fabricate or reconstruct base64, and do not call "
        "ingest_file for an attachment (its data_b64 is raw bytes you do not have). Copy the "
        "text verbatim, including any framing sentence ('Here's an article I want to keep:'); "
        "do NOT summarize or pre-extract facts. Set source_ref (filename/URL) so facts carry a "
        "citation. Processing runs in the BACKGROUND: returns immediately with status='pending' "
        "and eta_seconds; facts become searchable shortly after - call document_status once to "
        "confirm, then tell the user. Not for conversational messages - use remember_facts for "
        "those. A user document is one a HUMAN shared - never your own working text or session "
        "output; operational lessons YOU learn go to cortex_remember. May supersede."
    ),
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
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
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
                       "only the failed sections are reprocessed. Each queued document reports "
                       "its `accounting` class - `frozen_signature` means the chunk ledger was "
                       "EMPTY despite staged or committed facts (frozen counters/legacy shape): the retry "
                       "re-reads every section and re-stamps the ledger, it does NOT mean the "
                       "document was never retained. Use when a document's import report or "
                       "document_status shows failed sections, or the user asks to re-try/re-"
                       "import a document that did not fully land. Omit document_id to retry "
                       "every retriable document matching source_ref. Safe to call twice: an "
                       "already-queued document is reported as already active. Like "
                       "ingest_document, re-ingested facts may SUPERSEDE (archive) conflicting "
                       "facts already in memory.",
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
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            "required": []
        }
    },
    {
        "name": "document_status",
        # 2026-10-22 rewrite (same doc §3.3): gains the post-ingest confirmation trigger so
        # the "ask again in a moment" idle-wait advice has a one-call answer. 429/1024 served.
        "description": (
        # 957 chars (critic round 4): the round-2 prose grew this past the 1024 OpenAI
        # limit and _build_tool_openapi SILENTLY CLAMPED the served copy at 1021,
        # dropping the post-ingest trigger sentence from the OpenWebUI door and turning
        # four parity pins red. Budget is contractual - this text fits it whole, with
        # the witness, the retriable qualification AND the trigger sentence intact.
        "Report the import status of previously ingested documents: which are still processing, "
        "which finished only PARTIALLY (sections that could not be read, with the reason per "
        "failed section), and which are retriable via retry_document. Each document carries "
        "live chunk-ledger accounting - chunks_kept / chunks_owed / chunks_unread computed at "
        "read time (stored fact counters only move at finalize; trust these) - plus "
        "`accounting`: live; frozen_signature (empty ledger BUT staged or committed facts "
        "exist - frozen/legacy counters, NOT a genuinely empty document; retry_document "
        "repairs partial/error rows, while a stored-ready row with ledger-evidenced gaps "
        "reads status_effective=partial and is not yet retriable); or never_retained "
        "(honestly nothing extracted). Use when the user asks whether a document finished "
        "importing, what was missed, or what to re-try - and call this ONCE after any "
        "ingest_document call on an attached/pasted file to confirm it landed."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "Optional: report one document only. Omit for all."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
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
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
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
        # 2026-10-22 rewrite (same doc §3.2): the old text said "Use when the user shares…
        # a PDF/file attachment" — precisely what a chat client CANNOT honestly call (OWUI
        # delivers attachments as extracted <source> text, never bytes). 744/1024 served.
        "description": (
        "Upload a FILE (PDF or image) a HUMAN shared, as base64 in `data_b64`, when you HOLD "
        "THE RAW BYTES (API/script callers; a file on your own filesystem you can encode). "
        "FaultLine retains the bytes, extracts text deterministically (no model reads the "
        "file), and mines it exactly like ingest_document; embedded figures bind to surrounding "
        "text. NOT for chat attachments: in OpenWebUI the model receives an attachment as "
        "extracted <source> TEXT, never bytes - use ingest_document with that text instead. "
        "Never fabricate, truncate, or reconstruct base64: if you do not have the whole file "
        "encoded, you cannot call this. A file with no readable text (e.g. a photo) is still "
        "RETAINED and reported honestly. May supersede existing facts, like any ingest."
    ),
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
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
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
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
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
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
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
                       "chris@foo.com'. You MUST fill 'subject' (who the fact is about; 'me' for the "
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
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                }
            },
            # `old_value` IS required and the runtime refuses without it on all three doors.
            # Advertising it as optional made the MACHINE-READABLE half of this tool's contract
            # false: a schema-compliant call `{"subject": "me"}` is answered with HTTP 400.
            "required": ["subject", "old_value"]
        }
    },
    {
        "name": "cortex_remember",
        # WHEN, written for what the CALLER actually experiences. This used to lead with "a
        # command that failed ... a command overrun" — triggers only a coding agent ever meets.
        # A conversational assistant runs no commands, never recognised its own trigger, and
        # read the whole tool as "not for me": observed in production calling cortex_recall 5x and
        # cortex_remember 0x. A WHEN the consumer cannot recognise in itself is not a WHEN.
        # BUDGET: core + standing suffix must stay under 1024. Trimmed 734 -> ~500.
        #
        # ⚠️ THE `CALL THIS WHEN: ...` SENTENCES BELOW ARE EXCISED AT BUILD TIME when the pathway
        # layer is on — see premise._EXCISE_ANCHORS. They are restated almost verbatim by the
        # standing-rule suffix appended below, so with the pathway prefix added as well we would
        # be paying for the same trigger THREE times out of the tightest 1024-char budget on the
        # whole surface. Do not "fix" the duplication by editing the text here — the excision is
        # keyed to short anchors (start + end) and will no-op loudly if you move them.
        #
        # OWN-WORDS WRITE LANE (injection-vector fix): the note is written BY the agent, IN ITS
        # OWN WORDS — never a paste of the user's raw message, terminal output, or a transcript.
        # A note the agent composes itself cannot carry a user's pasted payload back into a
        # future session's context. Verbatim passing belongs to the USER-memory lane
        # (remember_facts) and nowhere else; if the user's exact words matter as a FACT, the
        # hand-off below routes them there.
        # RULE-FIRST SHAPE + CONTRAST PAIR (Build 4). A prose "in your own words" mandate
        # always loses to the verbatim message sitting right there in context — the model
        # needs to SEE the target, not be told about it. The GOOD/BAD pair below shows the
        # same lesson in both shapes: rule-first (situation -> action -> mechanism) vs an
        # owner-words transcription (no rule, no mechanism, first person noise). This is
        # standard tool-design practice (MCP "writing effective tools" / OpenAI function
        # guidance): concrete input examples beat abstract instruction. The pair must stay
        # SHORT — every char here is charged against the 1024 OpenAI cliff in BOTH pathway
        # states (measured 2026-08-16: 731 off / 718 on, suffix headroom ~300).
        "description": "Record a lesson about HOW YOU OPERATE. Supersedes older notes. CALL THIS WHEN: the user corrects you "
                       "or redirects you; you worked out how this user wants something done; "
                       "you hit a trap worth avoiding. Being corrected IS the trigger. "
                       "Lead with the RULE, in your own words: when <situation> arises, do "
                       "<action>, because <mechanism>. GOOD: 'Restart the worker, not the "
                       "whole stack — only the worker caches the schema.' BAD: 'He said stop "
                       "restarting everything, I told you twice.' "
                       "pay for twice. YOUR private working memory — not the user's fact; "
                       "their words as a fact belong in remember_facts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    # RULE-FIRST, not the user's words: the old field forbade paraphrasing and
                    # demanded the user's message byte-for-byte — a prompt-injection vector AND
                    # a transcription habit (the verbatim message sits right there in context
                    # and always wins against a prose "in your own words"). The fix is the
                    # SHAPE: the rule leads, the quote is demoted to its own optional field.
                    "description": "THE RULE, in your own words: when <situation> arises, do "
                                   "<action>, because <mechanism>. Lead with the rule — never "
                                   "with their words, output, or a transcript; a note you write "
                                   "yourself cannot carry their payload. If their exact wording "
                                   "matters, put it in owner_quote, and call remember_facts for "
                                   "anything that is a fact about them. Required — never leave "
                                   "empty."
                },
                "owner_quote": {
                    "type": "string",
                    "description": "OPTIONAL: their exact words, as a short quoted fragment — "
                                   "carried SECOND, behind the rule, never as the note body. "
                                   "Include only when the wording itself matters (a name, a "
                                   "value, a threshold); skip it otherwise."
                },
                "branch": {
                    "type": "string",
                    "description": "OPTIONAL: the branch or line of work this lesson was learned "
                                   "on (e.g. 'saas', 'main', a matter number). Notes learned on "
                                   "the branch you name at recall rank above equally-relevant "
                                   "others — it never hides anything."
                },
                "env": {
                    "type": "string",
                    "description": "OPTIONAL: the ENVIRONMENT this lesson is true in (e.g. "
                                   "'truenas', 'p70', 'local', a ward). Operational facts are "
                                   "often env-bound. Same-env notes rank first when you state "
                                   "your env at recall; other-env notes demote slightly and are "
                                   "STAMPED so you see why — nothing is ever hidden."
                },
                "category": {
                    "type": "string",
                    "enum": ["gotcha", "failed_command", "pitfall", "correction", "howto", "overrun"],
                    "description": "What KIND of lesson this is. Defaults to 'gotcha' if omitted."
                },
                # EVIDENCE LIVES HERE, not on remember_facts (2026-08-14 swap-fix): the
                # cortex handler (server.py cortex_remember_tool) has accepted an
                # `evidence: dict | None` param all along — the schema simply never
                # advertised it, while remember_facts (whose handler takes NO evidence
                # param and would TypeError on one, silently dropping the whole write)
                # advertised cortex's coaching instead. The pair was swapped; this is the
                # correct home. Field descriptions do NOT draw on the 1024 tool-description
                # budget — only the tool `description` does — but kept tight regardless.
                "evidence": {
                    "type": "object",
                    "description": "OPTIONAL proof — bind the lesson to the artifact that "
                                   "produced it: {\"sha\": \"<commit you checked it against>\", "
                                   "\"ref\": \"<bug/test id>\", \"command\": \"<what you ran>\", "
                                   "\"output\": \"<the actual result>\"}. A note is a CLAIM; a "
                                   "claim with no proof rots into a trap that sends a future you "
                                   "chasing work that was already done. With a `sha`, staleness "
                                   "becomes decidable (git merge-base --is-ancestor <sha> HEAD) "
                                   "and the note ranks ABOVE unevidenced ones on recall. Record "
                                   "it whenever you can."
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The triggers/topics this note guards, for just-in-time recall (e.g. ['docker','deploy'], ['git','tests'], ['llm-endpoint','config'])."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                },
                "client_source": {
                    "type": "string",
                    "description": "OPTIONAL: identify which CLIENT/TOOL is writing this note (e.g. 'claude-code', 'opencode'). This is the client, NOT the AI model — self-tag your model via `model`. Auto-stamped from the connection (clientInfo / mcp-name / User-Agent) when omitted; explicit wins. Lets a multi-model seat tell its own notes apart."
                },
                # SCHEMA UPLIFT (owner directive): provenance fields — free strings, never
                # enums; descriptions are budget-free (only the tool description counts).
                "model": {
                    "type": "string",
                    "description": "OPTIONAL: the configured MODEL doing the recording (e.g. 'qwen3.5-9b'). Auto-stamped from the deployment when omitted — pass it only to override. Rendered as provenance at recall; never a filter."
                },
                "type": {
                    "type": "string",
                    "description": "OPTIONAL: FREE-FORM owner-facing type of this note ('code', 'bug', 'operational', anything — loose by design, no vocabulary; emoji/hyphens/spaces fine). Complements, never replaces, the fixed engine `category` enum."
                },
                "project": {
                    "type": "string",
                    "description": "OPTIONAL: the PROJECT this note relates to (e.g. 'faultline', a repo name). A deployment default (CORTEX_PROJECT) is stamped when omitted; explicit wins. Rendered as provenance at recall."
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "cortex_recall",
        # OWN-QUERY READ LANE: the agent composes the query itself, describing the situation in
        # its own words — the old "pass the user's message VERBATIM" instruction made pasted user
        # content the retrieval key and pulled transcripts into the query slot. Keywords are
        # fine; the recall-first channel below is untouched (we removed an injection vector,
        # not the channel).
        "description": "Look up YOUR OWN operational notes before acting (gotchas, failed commands, "
                       "pitfalls, corrections, how-tos you recorded with cortex_remember). These are your "
                       "private working notes — hold them tentatively and use them to operate better; "
                       "they are NOT the user's facts. To recall what the USER told you, use recall_memory. "
                       "Before acting on a task like one you have done before, call cortex_recall FIRST — "
                       "before any recall_memory. Compose your OWN `query` — describe the task or "
                       "situation at hand in your own words; keywords are fine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Describe the situation or task at hand in YOUR OWN words — "
                                   "keywords are fine; do not paste the user's message. May be "
                                   "empty to list recent notes."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — BEST OMITTED: the transport binds the authenticated seat automatically (header-injected), and a value that does not match the token's own seat is rejected as a spoof with 403. Never invent, remember, or carry over a user_id from an earlier call — omit it unless the human explicitly gave you their UUID."
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "mine"],
                    "description": "OPTIONAL: 'mine' restricts results to notes tagged with this client PLUS untagged notes (untagged stay visible to everyone — never lose a lesson). Default 'all' returns every note from the seat."
                },
                "client_source": {
                    "type": "string",
                    "description": "OPTIONAL: identify which client/LLM/tool you are (e.g. 'claude-code', 'opencode'). Required when scope='mine' (otherwise 'mine' silently falls back to 'all'). Ignored when scope='all'."
                },
                "branch": {
                    "type": "string",
                    "description": "OPTIONAL: the branch or line of work you are on right now "
                                   "(e.g. 'saas', 'main'). Notes learned on that branch rank "
                                   "above equally-relevant others — it never hides anything."
                },
                "env": {
                    "type": "string",
                    "description": "OPTIONAL: the ENVIRONMENT you are operating in right now "
                                   "(e.g. 'truenas', 'p70', 'local'). Env-matching notes rank "
                                   "first; env-mismatched ones demote slightly and render "
                                   "stamped (e.g. '@truenas') so you see they may not apply "
                                   "here — untagged conceptual notes stay neutral."
                }
            },
            "required": []
        }
    }
]

# ── CANONICAL ORDER (2026-07-28 spec §tools): deterministic tool order is what enables client
# caching and stable prompt-cache hit rates — and ORDER IS DISCOVERABILITY for the models that
# read this list. The cortex pair (the agent's own second memory) used to trail the document/
# learn/retract lanes, so a model weighing tools met "store a document" before it met "recall
# your own notes" — the exact wrong priority for the agent-cortex loop, and the reason the Bar
# critic logs "reached for documents before cortex". Promoting the pair directly after the
# user-memory entry points makes the two-memory loop the THIRD thing a model learns about, not
# the seventh. Deterministic and declarative: reorder the tuple here, not by hand-moving dict
# blocks. Non-breaking — no client depends on list position, and `annotations`/`title`/
# `outputSchema`/premise/standing-rule application is name-keyed, so it is order-independent.
_CANONICAL_ORDER = (
    "recall_memory",      # user-memory READ entry point (premise carrier)
    "remember_facts",     # user-memory WRITE entry point (premise carrier)
    "cortex_recall",      # agent's own READ — before acting on a familiar task
    "cortex_remember",    # agent's own WRITE — being corrected IS the trigger
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


# Fold the SEEDED standing operating rules into the JSON-RPC tool descriptions from the SAME
# source as the OpenAPI (@app.post) surface, so the two never drift (memory-teaches-the-model,
# passive surface). No-op / byte-for-byte today's descriptions when the capability is off
# (open-core / SAAS-off): `tool_description_suffix` returns "". Fail-safe — a seed-import
# failure leaves TOOLS unchanged.
try:  # pragma: no cover - defensive; the seed module is pure-python (no DB)
    from src.api.cortex_usage_seed import tool_description_suffix as _tool_desc_suffix
    for _t in TOOLS:
        _suffix = _tool_desc_suffix(_t.get("name", ""))
        if _suffix:
            _t["description"] = _t.get("description", "") + _suffix
except Exception:
    pass

# THE PREMISE, PREFIXED onto the two entry-point tools.
#
# WHY HERE and not only in MCP `initialize`: `instructions` is the spec-correct slot, but it is a
# CLIENT-DEFINED carrier — many MCP clients drop it, and the OpenAPI door NEVER sees it. OpenWebUI
# (how our users' models actually connect) builds its function specs from per-operation
# `description`/`summary` ONLY and does not read `info.description` at all. So a premise that lives
# only in the handshake reaches an unknowable subset of clients and NONE of the OpenWebUI ones.
#
# A tool description is the one payload EVERY client on EVERY transport reads — it is the thing
# they exist to deliver. Applying it HERE (the single source both doors resolve from) reaches all
# of them without either transport knowing it exists.
#
# PREFIX, not suffix: a model reading a tool spec cold needs "why would I ever call this" BEFORE
# "how do I call it". The seeded standing rules above stay a SUFFIX — they are mechanics, and
# mechanics belong after the premise.
try:  # pragma: no cover - defensive; premise is a pure-python constant module
    from src.mcp.premise import tool_premise_prefix as _tool_premise_prefix
    for _t in TOOLS:
        _prefix = _tool_premise_prefix(_t.get("name", ""))
        if _prefix:
            _t["description"] = _prefix + _t.get("description", "")
except Exception:
    pass

# THE PATHWAY, PREFIXED onto the two cortex entry-point tools (cortex_recall, cortex_remember).
#
# WHY HERE AS WELL as the connect-time premise: `initialize.instructions` is a PROVEN carrier on
# MCP-native clients (verified — see premise.py) but it is CLIENT-DEFINED and many clients drop
# it, and the OpenAPI door never sees it at all. OpenWebUI builds its function specs from
# per-operation `description` ONLY. So for the door our users' models actually connect through,
# a tool description is the ONLY proactive channel that exists. Applying it in TOOLS — the single
# source both doors resolve from — reaches every client on every transport.
#
# WHY BOTH cortex tools and not one: the failure mode observed in production was a model calling
# cortex_recall 5x and cortex_remember 0x. A model reading `cortex_recall` alone learns a read tool; it must learn
# from that same text that a WRITE half exists, or the loop never closes. Symmetrically for a
# model that meets `cortex_remember` first.
#
# PREFIX, not suffix — same reason as the premise above: "does a loop like this exist" must land
# before "how do I call this one".
#
# Flag OFF (CORTEX_PATHWAY_DIRECTIONS=false) ⇒ `cortex_pathway_prefix` returns "" and every
# description is byte-for-byte the pre-feature text. Fail-safe: an import failure leaves TOOLS
# unchanged.
try:  # pragma: no cover - defensive; premise is a pure-python constant module
    from src.mcp.premise import apply_cortex_pathway as _apply_cortex_pathway
    for _t in TOOLS:
        _t["description"] = _apply_cortex_pathway(
            _t.get("name", ""), _t.get("description", ""))
except Exception:
    pass


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
    # front of the destructive divert is _serve_mode_blocks_divert, which is SAAS-only — on
    # open-core it is ungated. Demonstrated live, not inferred: a single recall_memory call
    # carrying "I do not have a pet X any more" wrote a fact, and forget_fact then reported
    # hard_delete on it.
    #
    # cortex_recall is the smaller version of the same overstatement: it defaults bump=True and
    # POSTs that to /cortex/recall, so an ordinary recall mutates engagement counters. Additive,
    # but not nothing.
    #
    # These hints are how a host decides what may run WITHOUT asking the user first, so claiming
    # read-only for a tool that can delete is the one direction of this lie that costs something.
    # The behaviour is deliberate and owner-ruled ("brain-not-transport"); the ANNOTATION was
    # simply wrong about it, and the annotation is the cheap half to correct.
    "recall_memory": {"readOnlyHint": False, "destructiveHint": True},
    "cortex_recall": {"readOnlyHint": False, "destructiveHint": False},
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
    #   • cortex_remember's backend note() marks earlier notes on the topic superseded_by the
    #     new one; they stay queryable but render as OUTDATED, so recall stops returning them
    #     as current.
    # None of that is a bug — it is the supersession model working. It simply is not "only
    # additive updates", which is what destructiveHint False tells a client.
    #
    # idempotentHint is claimed nowhere: a repeated ingest bumps confirmed_count, which can
    # advance a Class-B staged row's promotion by one confirmation.
    "remember_facts": {"readOnlyHint": False, "destructiveHint": True},
    "cortex_remember": {"readOnlyHint": False, "destructiveHint": True},
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
    "cortex_remember": "Remember Operational Note",
    "cortex_recall": "Recall Operational Notes",
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
    "cortex_remember": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Note-write outcome (noted, soft_error)."},
            "note": {"type": "string"},
        },
    },
    "cortex_recall": {
        "type": "object",
        "properties": {
            "memory": {"type": "string", "description": "My own operational notes as natural-language prose (held/tentative — never user fact)."},
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
