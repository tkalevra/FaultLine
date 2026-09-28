"""Transport-level markers for the /ingest write path — shared by BOTH sides of the HTTP
boundary (writers set them; src/api/main.py reads them).

Same pattern as src/api/llm_lane.py: a header that must mean the SAME thing to the writer
and the reader lives in ONE importable module, never as paired string literals in two
services (a literal pair drifts silently and the marker stops meaning anything).

DEFERRED-REPLAY MARKER (X-FL-Deferred-Replay)
─────────────────────────────────────────────
Set on EVERY re-send of content whose live ingest was ALREADY attempted once — TWO writer
families today:

1. the MCP seam's in-turn retry lane (attempt >= 2 in ``_ingest_with_retry``) and every
   deferred-worker POST (``_ingest_defer_worker`` / ``_DEFER_LANE_HEADERS``);
2. the re_embedder's episodic re-extract lane (``reextract_episodic``,
   src/re_embedder/embedder.py): a re-mined ``episodic_log`` row is BY CONSTRUCTION a
   replay — the turn's live ingest already ran when the row was captured, so its re-POST
   to /ingest is a re-send of already-attempted content, not a fresh user statement. The
   wound (2026-09-04): this lane re-ingested with preserved user_stated
   authority and NO marker, so a value /retract/correct had retired came back as a
   genuine-looking re-statement (history 'ingest_restate', superseded_at NULL). The
   marker now rides this lane too — gauntlet reextract-replay-resurrection.

NEVER on a first attempt: a first attempt IS a genuine write, indistinguishable from a
fresh user statement, and marking it would wrongly freeze rows.

The marker rides the HTTP header ONLY, never the request body: /ingest's idempotency key
hashes ``edges`` (src/api/idempotency.py) and the seam pins byte-identity between attempts,
so any body-borne marker would break the double-write guard to deliver the replay signal.

The backend reads it in ``main.py::ingest`` and threads ``replay=`` into
``FactStoreManager.commit`` (facts) and ``_commit_staged`` (staged_facts), where a replayed
upsert of a row the correction lane retired in the interleaving KEEPS its retired state and
its confirmed_count — while a genuine user RE-STATEMENT (fresh ingest, no marker) still
resurrects the row normally. User-is-truth untouched; replay freeze is not a promotion
freeze (the freeze applies only to the replayed upsert of an ALREADY-retired row).

REPLAY LANE HINT (X-FL-Replay-Lane)
───────────────────────────────────
OPTIONAL provenance detail a replaying writer MAY attach so the backend can attribute
holds to the lane that caused them (log fields, adjudication). Purely informational: an
absent, empty, or unrecognized value never changes the marker's decision — the marker
alone decides replay-ness, the lane hint only names the sender. Closed vocabulary, grown
only by adding a writer to this module.
"""

REPLAY_MARKER_HEADER = "X-FL-Deferred-Replay"
REPLAY_MARKER_VALUE = "1"

# Lane hint — informational only (see module docstring).
REPLAY_LANE_HEADER = "X-FL-Replay-Lane"
REPLAY_LANE_REEXTRACT = "reextract"  # the re_embedder episodic re-mine (embedder.py::reextract_episodic)

# Deliberately a tiny closed set, not a truthiness check on the raw string: an absent,
# empty, or unrecognized value must parse as NOT-a-replay (fail-safe toward the user's
# latest words — a garbled header must never freeze a row the user just re-stated).
_REPLAY_TRUTHY = frozenset({"1", "true", "yes"})


def replay_from_raw(raw: str | None) -> bool:
    """Parse the marker header value. Only the canonical forms above count."""
    return (raw or "").strip().lower() in _REPLAY_TRUTHY


def lane_from_raw(raw: str | None) -> str:
    """Parse the OPTIONAL lane hint. Unknown/absent -> "" (unattributed replay)."""
    return (raw or "").strip().lower()[:32]
