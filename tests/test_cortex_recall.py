"""Agent Cortex — SMART recall tests (semantic hybrid + provenance + decay).

Covers the three Builds layered onto the cortex's PRIVATE scratchpad recall (never the
user-truth walk):
  (a) SEMANTIC   — a note written WITHOUT descriptive tags is recalled by MEANING (cosine),
                   and an unrelated query is an honest miss ([]).  [needs a real embedder]
  (b) KEYWORD    — the deterministic keyword/tag lane still works (hybrid, not replaced).
  (c) PROVENANCE — a `correction` note outranks a `failed_command` note for an overlapping
                   query.
  (d) FAIL-SAFE  — with the embedding backend unreachable, recall still returns keyword/tag
                   results (no error, no block).
  (e) DECAY      — a stale, low-hit `failed_command` note decays; a `correction` (and a
                   hardened / high-hit note) never does.

Needs a reachable POSTGRES_DSN; skips cleanly otherwise. The semantic test additionally
needs a reachable embedding backend (fastembed / `/v1/embeddings`); it skips if absent
(run it inside the api container, which has fastembed cached).
"""

import os
import uuid

import pytest

from src.provisioning.schema_manager import create_cortex_schema
from src.api import cortex


# ── availability gates ─────────────────────────────────────────────────────────────────

def _db_available() -> bool:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
        conn.close()
        return True
    except Exception:
        return False


def _embedder_available() -> bool:
    try:
        v = cortex._embed_cortex_text("a short connectivity probe string", task="search_query")
        return bool(v) and len(v) > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no reachable POSTGRES_DSN")
_needs_embedder = pytest.mark.skipif(
    not _embedder_available(), reason="no reachable embedding backend (fastembed / /v1/embeddings)"
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────

def _fresh_uuid() -> str:
    return str(uuid.uuid4())


def _drop_schema(schema: str) -> None:
    import psycopg2
    try:
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _row(schema: str, note_like: str) -> dict:
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "SELECT category, lifecycle_state, observation_count, hit_count,"
                "       (embedding IS NOT NULL)"
                " FROM cortex_episodes WHERE note ILIKE %s ORDER BY id DESC LIMIT 1",
                (f"%{note_like}%",),
            )
            r = cur.fetchone()
        return None if not r else {
            "category": r[0], "lifecycle_state": r[1], "observation_count": r[2],
            "hit_count": r[3], "has_embedding": r[4],
        }
    finally:
        conn.close()


def _mutate(schema: str, note_like: str, sql_set: str) -> None:
    """Directly poke a row's counters/timestamps to simulate age/usage for the decay test."""
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                f"UPDATE cortex_episodes SET {sql_set} WHERE note ILIKE %s",
                (f"%{note_like}%",),
            )
        conn.commit()
    finally:
        conn.close()


def _ranks(hits: list, note_substr: str) -> int:
    for i, h in enumerate(hits):
        if note_substr.lower() in h["note"].lower():
            return i
    return -1


# ── (f) TIME + BRANCH (Build 4) — the stale-vs-fresh replay, pinned ─────────────────────
#
# THE REPLAYED FAILURE (real pair from this repo's operating history; see
# DEV/replay_stale_vs_fresh.py for the standalone artifact): a stale HOWTO hardened by
# repetition and carrying evidence taught "deploy = docker compose up from the repo dir";
# the fresh CORRECTION (after it displaced the prod stack) forbade exactly that. Under
# time-blind ranking the howto won: hardened(+2.0) + evidence(+1.5) on a 1.5-weight howto
# beat a bare 3.0-weight correction, and every recall BUMP reset last_seen_at so the stale
# winner laundered its own age forever. These tests pin the fix: recency completes the
# Park et al. (recency x importance x relevance) triad, the hardened bonus decays with the
# write clock, a correction dominates (Koren 2009 temporal dynamics), and procedure that
# predates the newest correction on the same topic is demoted (never dropped).

_OLD_HOWTO = ("To deploy, run docker compose up from the repo directory; it rebuilds the "
              "image and restarts the stack in one step.")
_NEW_CORRECTION = ("Never docker compose up from the repo directory for a deploy — it targets "
                   "the wrong stack and displaces prod; rsync to the build host and "
                   "redeploy via the stack manager.")


def _write_real_pair(uid, howto_age_days, howto_obs=4):
    """Write the real howto/correction pair; age the howto and harden it by repetition."""
    cortex.note(uid, _OLD_HOWTO, category="howto", tags=["deploy", "docker", "stack"],
                evidence={"sha": "77b563e7" * 4, "ref": "deploy runbook"})
    cortex.note(uid, _NEW_CORRECTION, category="correction", tags=["deploy", "docker", "stack"])
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {cortex.cortex_schema_for(uid)}")
            cur.execute(
                "UPDATE cortex_episodes"
                " SET first_seen_at = now() - (%s || ' days')::interval,"
                "     last_seen_at  = now() - (%s || ' days')::interval,"
                "     observation_count = %s"
                " WHERE note ILIKE %s",
                (str(howto_age_days), str(howto_age_days), howto_obs, "%run docker compose up%"),
            )
        conn.commit()
    finally:
        conn.close()


def _winner_category(uid, query="deploy docker stack"):
    hits = cortex.recall_notes(uid, query=query, limit=3, bump=False)
    return hits[0]["category"] if hits else "none"


def test_fresh_correction_outranks_stale_hardened_howto(monkeypatch):
    """THE REPLAY, as a pin. The stale hardened evidenced howto must lose to the fresh
    correction that superseded it, and repeated bumping recalls must not flip it back
    (the bump used to reset last_seen_at, laundering the stale note's age)."""
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        _write_real_pair(uid, howto_age_days=90, howto_obs=4)
        assert _winner_category(uid) == "correction"
        # the stale note STILL SURFACES (demoted, never dropped): ranking signals, no gates.
        hits = cortex.recall_notes(uid, query="deploy docker stack", limit=3, bump=False)
        assert any("run docker compose up" in h["note"] for h in hits), (
            "the stale howto was DROPPED — time/branch signals must reorder, never gate"
        )
        # repeated attended recalls (bump=True) must not flip the winner.
        for _ in range(3):
            cortex.recall_notes(uid, query="deploy docker stack", limit=3, bump=True)
        assert _winner_category(uid) == "correction"
    finally:
        _drop_schema(schema)


@pytest.mark.parametrize("age_days", [1, 7, 30, 90, 365])
def test_correction_wins_at_every_age_gap(monkeypatch, age_days):
    """WIN EVERY TIME: the correction wins across the whole age range, down to a one-day
    gap (where recency alone barely separates them — supersession carries it)."""
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        _write_real_pair(uid, howto_age_days=age_days)
        assert _winner_category(uid) == "correction", (
            f"howto aged {age_days}d beat the fresh correction"
        )
    finally:
        _drop_schema(schema)


def test_read_does_not_launder_age(monkeypatch):
    """WRITE-CLOCK DISCIPLINE: a bumping recall advances hit_count but MUST NOT reset
    last_seen_at — a read that launders age pins a stale note at the head of the window
    forever (the mechanism behind the replayed failure)."""
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        cortex.note(uid, "an old lesson whose clock must not move on read", tags=["clock"])
        _mutate(schema, "clock must not move",
                "last_seen_at = now() - INTERVAL '90 days', observation_count = 1")
        before = _row(schema, "clock must not move")
        assert before["lifecycle_state"] == "observed"
        cortex.recall_notes(uid, query="clock lesson", limit=3, bump=True)
        after = _row(schema, "clock must not move")
        assert after["hit_count"] == before["hit_count"] + 1, "bump must still count hits"
        # last_seen_at unchanged (still ~90 days ago, not today) — checked in SQL below.
        import psycopg2
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute("SELECT last_seen_at < now() - INTERVAL '80 days'"
                            " FROM cortex_episodes WHERE note ILIKE '%clock must not move%'")
                assert cur.fetchone()[0] is True, (
                    "a READ reset last_seen_at — age laundering is back"
                )
        finally:
            conn.close()
    finally:
        _drop_schema(schema)


def test_branch_match_ranks_same_branch_first(monkeypatch):
    """BRANCH SIGNAL: with two equally-relevant notes, the one learned on the branch being
    worked ranks first. Mismatch is never a penalty (no gate): the other-branch note still
    surfaces, merely second."""
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        cortex.note(uid, "same procedure, learned on another line of work", tags=["proc"],
                    branch="feature/other")
        cortex.note(uid, "same procedure, learned on the line being worked", tags=["proc"],
                    branch="saas")
        hits = cortex.recall_notes(uid, query="procedure", limit=3, bump=False, branch="saas")
        assert hits[0]["note"].startswith("same procedure, learned on the line")
        assert any("another line of work" in h["note"] for h in hits), (
            "an other-branch note was dropped — branch must rank, never gate"
        )
        # branch appears in the record (ranking transparency).
        assert hits[0]["branch"] == "saas"
        # no branch stated: no ordering change is required, but nothing may vanish.
        assert cortex.recall_notes(uid, query="procedure", limit=3, bump=False)
    finally:
        _drop_schema(schema)


def test_owner_quote_rides_second_and_rule_leads(monkeypatch):
    """RULE-FIRST SHAPE, verified on the STORED ROW (never the write response): the note
    body leads with the distilled rule and does NOT contain the owner's quoted words;
    the quote rides in context.owner_quote; the branch lands in its own column."""
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        quote = "you should be recording roadblocks and errors to cortex, and moreover asking beforehand"
        rule = ("Before standing up test infrastructure or touching an environment "
                "dependency, run a cortex recall on the task first — past sessions "
                "already paid for the traps, and a roadblock discovered by replay costs the "
                "hour the note was written to save; record each roadblock as it lands.")
        assert cortex.note(uid, rule, category="correction", tags=["cortex", "workflow"],
                           branch="saas", owner_quote=quote)
        import psycopg2
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "SELECT note, context->>'owner_quote', branch FROM cortex_episodes"
                    " WHERE category = 'correction'")
                row = cur.fetchone()
        finally:
            conn.close()
        note_text, stored_quote, stored_branch = row
        assert note_text.startswith("Before standing up"), "the rule must LEAD the note body"
        assert quote not in note_text, "the owner's words must not be transcribed into the note"
        assert stored_quote == quote, "the quote rides in context.owner_quote"
        assert stored_branch == "saas", "the branch lands in its own column"
    finally:
        _drop_schema(schema)


# ── (a) SEMANTIC — tag-free recall by MEANING ────────────────────────────────────────────

@_needs_embedder
def test_semantic_recall_tag_free():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        # NO descriptive tags — the only bridge to the query is MEANING (embedding cosine).
        assert cortex.note(uid, "LLM endpoint is 198.51.100.20:8080, NOT :1234",
                           category="gotcha", tags=[])
        assert cortex.note(uid, "new files under tests/ are gitignored, use git add -f",
                           category="pitfall", tags=[])
        # embeddings must actually have been stored at write.
        assert _row(schema, "198.51.100.20")["has_embedding"] is True
        # Query shares NO exact word with the note text; only semantics connect them.
        hits = cortex.recall_notes(uid, "what port for the model", limit=5)
        assert hits, "semantic recall returned nothing on a tag-free note"
        assert "198.51.100.20" in hits[0]["note"], f"wrong note on top: {hits[0]['note']!r}"
        # an unrelated query is an HONEST MISS — no keyword, sub-floor cosine → nothing.
        assert cortex.recall_notes(uid, "unrelated aquarium submarine dolphin") == []
    finally:
        _drop_schema(schema)


# ── (b) KEYWORD/TAG lane still works (hybrid, not replaced) ───────────────────────────────

def test_keyword_tag_recall_still_works(monkeypatch):
    # Force the embedder OFF so this asserts the pure keyword/tag lane deterministically.
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        cortex.note(uid, "LLM endpoint is 198.51.100.20:8080, NOT :1234",
                    category="gotcha", tags=["llm-endpoint", "config", "port", "model"])
        cortex.note(uid, "new files under tests/ are gitignored, use git add -f",
                    category="pitfall", tags=["git", "tests"])
        hits = cortex.recall_notes(uid, "what port for the model", limit=5)
        assert hits and "198.51.100.20" in hits[0]["note"]
        # honest miss with no embedder and no overlap.
        assert cortex.recall_notes(uid, "unrelated aquarium submarine") == []
    finally:
        _drop_schema(schema)


# ── (c) PROVENANCE — correction outranks failed_command on an overlapping query ──────────

def test_correction_outranks_failed_command(monkeypatch):
    # Embedder OFF → isolate the provenance weight from any semantic tie-break.
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        # Same discriminating tag "deploy" on both → equal keyword overlap for the query.
        cortex.note(uid, "the deploy needs a full stack recreate to adopt a new image",
                    category="correction", tags=["deploy"])
        cortex.note(uid, "deploy command exited non-zero once on a flaky mount",
                    category="failed_command", tags=["deploy"])
        hits = cortex.recall_notes(uid, "deploy", limit=5)
        r_corr = _ranks(hits, "full stack recreate")
        r_fail = _ranks(hits, "flaky mount")
        assert r_corr >= 0 and r_fail >= 0, f"both notes must surface: {hits}"
        assert r_corr < r_fail, f"correction must outrank failed_command (corr={r_corr}, fail={r_fail})"
    finally:
        _drop_schema(schema)


# ── (d) FAIL-SAFE — embedding backend unreachable → keyword/tag still returns ────────────

def test_failsafe_embedding_unavailable(monkeypatch):
    # Simulate the embedding backend being DOWN at both write and recall.
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        assert cortex.note(uid, "LLM endpoint is 198.51.100.20:8080, NOT :1234",
                           category="gotcha", tags=["llm-endpoint", "port", "model"])
        # No embedding was stored (backend down) — recall must NOT error and must still work.
        assert _row(schema, "198.51.100.20")["has_embedding"] is False
        hits = cortex.recall_notes(uid, "what port for the model", limit=5)
        assert hits, "fail-safe keyword/tag recall returned nothing"
        assert "198.51.100.20" in hits[0]["note"]
    finally:
        _drop_schema(schema)


# ── (e) DECAY — stale low-hit failed_command decays; correction / hardened / high-hit stay ─

def test_decay_drops_stale_lowhit_failed_command_keeps_gold(monkeypatch):
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        cortex.note(uid, "stale failed_command that nobody ever recalls",
                    category="failed_command", tags=["x"])
        cortex.note(uid, "a user correction that must stay durable forever",
                    category="correction", tags=["y"])
        cortex.note(uid, "an old failed_command that is still recalled a lot",
                    category="failed_command", tags=["z"])
        # Age ALL three well past the decay window and zero-out the stale one's hits.
        old = "last_seen_at = now() - INTERVAL '90 days', hit_count = 0, observation_count = 1"
        _mutate(schema, "nobody ever recalls", old)
        _mutate(schema, "must stay durable", "last_seen_at = now() - INTERVAL '90 days', hit_count = 0, observation_count = 1")
        # The high-hit failed_command is old but frequently recalled → must survive.
        _mutate(schema, "still recalled a lot", "last_seen_at = now() - INTERVAL '90 days', hit_count = 25, observation_count = 1")

        res = cortex.sweep_lifecycle_for_user(uid)
        assert res["decayed"] >= 1

        # gold (correction) NEVER decays, even when stale.
        assert _row(schema, "must stay durable")["lifecycle_state"] != "decayed"
        # high-hit failed_command survives (rarely-recalled is the decay signal, not age alone).
        assert _row(schema, "still recalled a lot")["lifecycle_state"] != "decayed"
        # the stale, low-hit failed_command decays.
        assert _row(schema, "nobody ever recalls")["lifecycle_state"] == "decayed"
        # a decayed note is no longer recalled.
        assert _ranks(cortex.recall_notes(uid, "nobody"), "nobody ever recalls") == -1
    finally:
        _drop_schema(schema)


# ── (g) BUILD 5 — env scoping, supersession links, staleness, authorship ───────────────
#
# Owner design intent (2026-08-16): the cortex is the agents' own memory. These pin the
# four Build-5 capabilities, all under the standing rule: everything RANKS and STAMPS,
# nothing GATES. Notably the cortex IS a vector store by nature (semantic match may be the
# only match); these signals reorder and annotate what the lanes surface.


def _env_pair(uid):
    """Two equally-relevant deploy notes, one bound to truenas, one to p70 (distinct texts —
    the dedup key is (category, md5(note)), so identical bodies would collapse to one row)."""
    cortex.note(uid, "recreate the whole stack when adopting a new image on the shared NAS host",
                category="howto", tags=["deploy", "stack"], env="truenas")
    cortex.note(uid, "recreate the whole stack when adopting a new image on the workstation",
                category="howto", tags=["deploy", "stack"], env="p70")


def test_env_match_ranks_first_and_mismatch_never_hides(monkeypatch):
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        _env_pair(uid)
        hits = cortex.recall_notes(uid, query="deploy stack image", limit=5, bump=False,
                                   env="truenas")
        assert hits, "env recall returned nothing"
        # the env-matching note ranks FIRST...
        assert hits[0]["env"] == "truenas", f"env match must rank first: {hits[0]['env']}"
        # ...and the OTHER-env note still SURFACES (demoted, never gated).
        assert any(h.get("env") == "p70" for h in hits), (
            "the p70 note was hidden — env must rank, never gate"
        )
        # untagged env (no env stated): no ordering requirement, nothing hidden.
        neutral = cortex.recall_notes(uid, query="deploy stack image", limit=5, bump=False)
        assert len(neutral) >= 2
    finally:
        _drop_schema(schema)


def test_supersession_tombstones_the_replaced_note(monkeypatch):
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        # the OLD procedure (shares tag + tokens with the correction below)
        cortex.note(uid, "to deploy, run docker compose up from the repo directory",
                    category="howto", tags=["deploy", "docker"])
        hits = cortex.recall_notes(uid, query="deploy docker compose", limit=5, bump=False)
        assert any("repo directory" in h["note"] for h in hits)
        # the CORRECTION lands -> the old note must be tombstone-linked to it
        cortex.note(uid, "never run docker compose up from the repo directory to deploy; "
                         "build on the host and redeploy via the stack manager",
                    category="correction", tags=["deploy", "docker"])
        import psycopg2
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "SELECT c.note, sup.note FROM cortex_episodes c"
                    " LEFT JOIN cortex_episodes sup ON sup.id = c.superseded_by"
                    " WHERE c.category = 'howto' AND c.superseded_by IS NOT NULL")
                row = cur.fetchone()
        finally:
            conn.close()
        assert row, "the howto was not tombstoned by the correction"
        old_note, sup_note = row
        assert "repo directory" in old_note
        assert "never run docker compose up" in sup_note, (
            "the tombstone must point at the correction that replaced it"
        )
        # the rec carries WHAT replaced it, so the render can explain the transition.
        hits = cortex.recall_notes(uid, query="deploy docker compose", limit=5, bump=False)
        old_rec = next(h for h in hits if "repo directory" in h["note"] and h["category"] == "howto")
        assert old_rec.get("superseded_by_note") and "never run" in old_rec["superseded_by_note"]
        # DISJOINT notes never link (conservative guard).
        cortex.note(uid, "a totally unrelated lesson about tea temperatures",
                    category="correction", tags=["deploy", "docker"])
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "SELECT count(*) FROM cortex_episodes"
                    " WHERE note LIKE '%tea temperatures%' AND superseded_by IS NOT NULL")
                assert cur.fetchone()[0] == 0, "disjoint note was wrongly tombstoned"
        finally:
            conn.close()
    finally:
        _drop_schema(schema)


def test_stale_unevidenced_note_is_marked_old_in_render_path():
    """The render-side staleness stamp (mcp server helper): evidenced notes never stale-mark
    (a sha makes staleness DECIDABLE); unevidenced + old -> loud suffix; fresh -> silent."""
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.mcp import server as mcp_server
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    assert mcp_server._cortex_stale_suffix(
        {"evidence_sha": "abc123", "last_seen_at": old}) == ""
    assert "stale" in mcp_server._cortex_stale_suffix({"last_seen_at": old})
    assert mcp_server._cortex_stale_suffix({"last_seen_at": None}) == ""
    fresh = datetime.now(timezone.utc).isoformat()
    assert mcp_server._cortex_stale_suffix({"last_seen_at": fresh}) == ""


@pytest.mark.asyncio
async def test_cortex_remember_auto_stamps_authorship(monkeypatch):
    """WHO wrote the note: when the model does not pass client_source, the edge's captured
    client identity (mcp-name / User-Agent / stdio clientInfo) is stamped automatically —
    zero model effort, every note carries its author. (Experience lane stubbed so the
    payload under test is the single /cortex/note write.)"""
    from unittest.mock import AsyncMock, MagicMock, patch
    import src.mcp.server as m
    async def _no_experience(*a, **k):
        return []
    monkeypatch.setattr(m, "_fetch_experience_lines", _no_experience)
    import src.mcp.server as m
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"written": True, "status": "stored"})
    mock_http_client = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)
    with patch("src.mcp.server._http_client", mock_http_client),          patch("src.mcp.server._client_class") as cc:
        cc.current_client_name.return_value = "glm-5.2"
        await m.cortex_remember_tool(
            text="When the deploy must adopt a new image, recreate the stack.",
            user_id="11111111-1111-1111-1111-111111111111",
            category="howto", tags=["deploy"], env="truenas",
        )
    _, kwargs = mock_http_client.post.call_args
    assert kwargs["json"]["client_source"] == "glm-5.2", "authorship must auto-stamp"
    assert kwargs["json"]["env"] == "truenas", "env must ride the write payload"
    # explicit identity always wins over the auto-stamp
    with patch("src.mcp.server._http_client", mock_http_client),          patch("src.mcp.server._client_class") as cc:
        cc.current_client_name.return_value = "auto-detected"
        await m.cortex_remember_tool(
            text="Another rule-shaped lesson for identity precedence.",
            user_id="11111111-1111-1111-1111-111111111111",
            client_source="explicit-wins",
        )
    _, kwargs = mock_http_client.post.call_args
    assert kwargs["json"]["client_source"] == "explicit-wins"

# ── note-admission gate: deterministic-only, LLM-free by construction ───────────────────

def test_note_admission_gate_deterministic_and_llm_free():
    """The admission gate is a free, fail-closed SHAPE test — no LLM stage exists.

    (1) real own-words operational prose is admitted;
    (2) a pasted multi-line chat transcript and a fenced code block are refused;
    (3) the gate signature accepts no LLM classifier and the cortex module's write path
        involves no LLM chat call at all (source-level assertion — honest and cheap).
    """
    import inspect

    # (1) own-words operational prose is admitted.
    adm = cortex.classify_note_admission(
        "Never run the migration without a snapshot check first; it wedges the schema lock.")
    assert adm["admit"] is True
    assert adm["lane"] == "instruction"
    assert adm["via"] == "heuristic"

    # (2a) a pasted multi-line chat transcript is refused as DATA.
    transcript = (
        "user: how do I fix the rate limit?\n"
        "assistant: you should back off and retry with jitter.\n"
        "user: ok thanks"
    )
    adm = cortex.classify_note_admission(transcript)
    assert adm["admit"] is False
    assert adm["lane"] == "data"
    assert adm["reason"] == "conversation_transcript"

    # (2b) a fenced code block is refused as DATA.
    adm = cortex.classify_note_admission("Use this fix:\n```bash\nexport FOO=1\n```")
    assert adm["admit"] is False
    assert adm["reason"] == "code_block"

    # (3) the gate signature takes NO LLM classifier, and the cortex module source contains
    # no call_llm reference — the write path is LLM-free by construction.
    params = list(inspect.signature(cortex.classify_note_admission).parameters)
    assert params == ["text", "category"]
    src = inspect.getsource(cortex)
    assert "call_llm" not in src, "cortex module must never call an LLM chat endpoint"
