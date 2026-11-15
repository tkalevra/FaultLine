"""Assistant-turn capture — THE INPUT LANE IS THE USER'S (owner-ratified correction).

Everything ingested from the chat/conversation — the user's OWN turns AND the
assistant's turns — is the user's grounded session. Assistant-authored statements are
therefore captured through the SAME extraction pipeline as user statements and tagged
fact_provenance="user_stated" (Class A for a structured fact — FIX A; a C-defined
transient/feeling rel stays durable B), so they:

  * land Class A for a structured (A- or B-defined) rel (never buried at B/C the way
    engine growth is), and
  * carry EQUAL user authority — an assistant chat fact coexists / supersedes with a
    user fact normally (same rank), and a user correction still wins.

The router does this by mapping source="assistant" → user_stated exactly like
source="mcp" (the user's own turns) — see the provenance router in main.py.

The earlier R3 design downgraded assistant turns to llm_learned (Class B/C, firewalled)
on the fallacy that chat input might not be the user's. It is — there is no
"someone else's words" input lane. THAT downgrade is what these tests reverse.

The TRUST FIREWALL is UNCHANGED and still correct for its real target: the ENGINE'S
OWN GROWTH — llm_learned (source="llm_learn": /expand, WordNet ladders, ±6 buildout)
and llm_inferred (inference) — still ranks below user_stated and is dropped vs a user
fact by _decide_supersede_or_coexist. Those firewall pins below now stand for ENGINE
inference, NOT chat content.

Pure-function pins on the load-bearing seams:
  - assign_class_and_confidence  (user_stated → A for an A-rel; llm_learned engine → B/C)
  - _provenance_rank             (authority ladder user_stated > llm_learned > llm_inferred)
  - _decide_supersede_or_coexist (firewall: engine inference never beats a user fact;
                                  user-vs-user chat coexists)
  - _partition_chunk_by_role     (deterministic transcript role split, document lane)
"""
import pytest

from src.api import main


# ─────────────────────── fixtures ───────────────────────

@pytest.fixture
def _seed_rel_meta(monkeypatch):
    """Seed the in-process rel metadata cache so assign_class_and_confidence resolves a
    defined fact_class without a DB (the caches are DB-populated at runtime)."""
    def _set(rel, fact_class):
        monkeypatch.setitem(main._REL_TYPE_META, rel, {"fact_class": fact_class})
        if rel in getattr(main, "_REL_TYPE_CACHE", {}):
            monkeypatch.delitem(main._REL_TYPE_CACHE, rel, raising=False)
    return _set


# ─────────────── assistant chat content → user_stated (Class A for an A-rel) ───────────────
# The router maps source="assistant" → user_stated, so assistant-turn facts run
# assign_class_and_confidence with fact_provenance="user_stated" — identical to the
# user's own turns (source="mcp"). These pin the resulting tier.

def test_assistant_chat_a_rel_lands_class_a(_seed_rel_meta):
    """A cleanly-classifying assistant statement on an A-rel (e.g. instance_of) lands
    Class A — user authority, NOT buried at B the way the old firewall did."""
    _seed_rel_meta("instance_of", "A")
    cls, conf = main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance="user_stated", rel_type="instance_of")
    assert cls == "A"
    assert conf == pytest.approx(1.0)


def test_assistant_chat_b_rel_lands_class_a(_seed_rel_meta):
    """FIX A (PROVENANCE PROMOTES): a user_stated statement on a B-defined rel now rises to
    Class A — the seeded fact_class floors INFERENCE only, it must not degrade user truth.
    (Was Class B pre-fix; the ``else Class B`` degraded structured user truth to staged.)"""
    _seed_rel_meta("located_in", "B")
    cls, conf = main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance="user_stated", rel_type="located_in")
    assert cls == "A"
    assert conf == pytest.approx(1.0)


def test_assistant_chat_c_rel_lands_at_least_b(_seed_rel_meta):
    """Even a C-defined rel, when USER-STATED (assistant chat is user_stated), floors at
    Class B — user authority never falls to the short-term C net for a stated fact."""
    _seed_rel_meta("feels", "C")
    cls, conf = main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance="user_stated", rel_type="feels")
    assert cls == "B"
    assert conf == pytest.approx(0.8)


# ─────────────── ENGINE growth (llm_learned) still tiers B/C, never A ───────────────
# UNCHANGED firewall target: /expand / WordNet / ±6 buildout (source="llm_learn") rides
# llm_learned and must never reach Class A.

def test_engine_llm_learned_a_rel_capped_at_b(_seed_rel_meta):
    _seed_rel_meta("instance_of", "A")
    cls, conf = main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance="llm_learned", rel_type="instance_of")
    assert cls == "B"
    assert conf == pytest.approx(0.6)


def test_engine_llm_learned_c_rel_stays_c(_seed_rel_meta):
    _seed_rel_meta("feels", "C")
    cls, _ = main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance="llm_learned", rel_type="feels")
    assert cls == "C"


def test_engine_llm_learned_never_class_a_for_any_shape(_seed_rel_meta):
    """No combination of engine llm_learned + rel metadata may yield Class A."""
    for fc in ("A", "B", "C"):
        _seed_rel_meta("some_rel", fc)
        cls, _ = main.assign_class_and_confidence(
            {"storage": "relational"}, fact_provenance="llm_learned", rel_type="some_rel")
        assert cls != "A", f"engine llm_learned defined_class={fc} must never be Class A"


# ─────────────── authority ladder (unchanged) ───────────────

def test_provenance_authority_ordering():
    assert main._provenance_rank("user_stated") > main._provenance_rank("llm_learned")
    assert main._provenance_rank("llm_learned") > main._provenance_rank("llm_inferred")
    assert main._provenance_rank("garbage") == 0  # unknown → lowest, safe


# ─────────────── THE FIREWALL still guards ENGINE inference vs a user fact ───────────────
# These now stand for the ENGINE'S OWN growth (llm_learned from /expand, llm_inferred
# from inference), NOT chat content. The firewall drops them vs a higher-authority user
# fact — that is exactly the behavior we must NOT weaken.

def test_firewall_engine_llm_learned_dropped_vs_user_state_rel():
    """State rel: an ENGINE (llm_learned) fact conflicting with a user_stated fact is
    DROPPED — it may not even coexist-compete as current state."""
    outcome = main._decide_supersede_or_coexist(
        {"fact_provenance": "llm_learned", "is_correction": False},
        {"fact_provenance": "user_stated"},
        {"temporal_class": "state"},
    )
    assert outcome == "drop_incoming"


def test_firewall_engine_llm_learned_dropped_vs_user_immutable_rel():
    """Immutable rel: engine fact loses to a higher-authority user fact."""
    outcome = main._decide_supersede_or_coexist(
        {"fact_provenance": "llm_learned", "is_correction": False},
        {"fact_provenance": "user_stated"},
        {"temporal_class": "immutable"},
    )
    assert outcome == "drop_incoming"


def test_firewall_llm_inferred_also_dropped_vs_user():
    """Any strictly-lower authority (llm_inferred inference too) loses to a user fact."""
    outcome = main._decide_supersede_or_coexist(
        {"fact_provenance": "llm_inferred", "is_correction": False},
        {"fact_provenance": "user_stated"},
        {"temporal_class": "state"},
    )
    assert outcome == "drop_incoming"


# ─────────────── chat-assistant fact (now user_stated) coexists with a user turn ───────────────

def test_assistant_chat_coexists_with_user_state():
    """An assistant chat fact is user_stated → EQUAL authority. Against an existing user
    state fact it COEXISTS (never dropped, never hidden) — history preserved, recency
    resolves current. This is the direct behavioral flip from the old firewall drop."""
    outcome = main._decide_supersede_or_coexist(
        {"fact_provenance": "user_stated", "is_correction": False},
        {"fact_provenance": "user_stated"},
        {"temporal_class": "state"},
    )
    assert outcome == "coexist"


def test_user_fact_supersedes_engine_fact_immutable():
    """A user statement overriding an ENGINE (llm_learned) fact WINS (immutable rel)."""
    outcome = main._decide_supersede_or_coexist(
        {"fact_provenance": "user_stated", "is_correction": False},
        {"fact_provenance": "llm_learned"},
        {"temporal_class": "immutable"},
    )
    assert outcome == "supersede_existing"


def test_correction_always_supersedes():
    """A user correction still supersedes regardless of the firewall guard."""
    outcome = main._decide_supersede_or_coexist(
        {"fact_provenance": "user_stated", "is_correction": True},
        {"fact_provenance": "user_stated"},
        {"temporal_class": "state"},
    )
    assert outcome == "supersede_existing"


# ─────────────── document-lane role split (deterministic, no LLM) ───────────────
# The split is retained so the assistant lane inherits the shared date header for
# grounding; BOTH lanes now ingest as user_stated (source mcp / assistant both map there).

def test_partition_splits_user_and_assistant():
    from src.re_embedder.embedder import _partition_chunk_by_role
    chunk = ("[Date: 2023-05-01]\n"
             "user: Any good Italian spots?\n"
             "assistant: I'd recommend Roscioli — great carbonara.")
    user_text, asst_text = _partition_chunk_by_role(chunk)
    assert "Any good Italian spots?" in user_text
    assert "Roscioli" not in user_text          # assistant content must NOT leak into user lane
    assert "Roscioli" in asst_text
    assert "[Date: 2023-05-01]" in asst_text     # date shared so assistant facts are grounded
    assert "assistant:" not in asst_text         # prefix stripped


def test_partition_plain_document_is_user_lane_only():
    """A plain (non-conversational) document has no assistant prefix → whole text is
    the user lane and the assistant lane is empty (legacy byte-identical behavior)."""
    from src.re_embedder.embedder import _partition_chunk_by_role
    chunk = ("Photosynthesis converts carbon dioxide and water into glucose and oxygen.\n"
             "It occurs in the chloroplast.")
    user_text, asst_text = _partition_chunk_by_role(chunk)
    assert "Photosynthesis" in user_text
    assert "chloroplast" in user_text
    assert asst_text == ""


def test_partition_unprefixed_continuation_follows_user_by_default():
    from src.re_embedder.embedder import _partition_chunk_by_role
    chunk = "I moved to Berlin last year.\nassistant: That's a great city."
    user_text, asst_text = _partition_chunk_by_role(chunk)
    assert "Berlin" in user_text
    assert "great city" in asst_text
    assert "Berlin" not in asst_text


def test_partition_empty_chunk():
    from src.re_embedder.embedder import _partition_chunk_by_role
    assert _partition_chunk_by_role("") == ("", "")
    assert _partition_chunk_by_role("   \n  ") == ("", "")
