"""[it branch] FOSS-011 - a FIRST-MENTION naming sentence is a STATEMENT, never a CORRECTION.

Battery sentence (the bug repro, deterministic on the it stack - classified CORRECTION
twice, minting a "server" entity and destructively flipping pref_name server-to-devbox while
capturing NO relational content and NO IP):

    it: "Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5."
    es: "Mi servidor principal se llama DevBox, su IP es 10.0.1.5."   (measured: STATEMENT)
    en: "My main server is called DevBox, its IP is 10.0.1.5."        (measured: STATEMENT)

The fix aligns the it intent pattern set with the measured es/en behavior and adds the
doc first-mention guard, at four seams:

  1. linguistics.licensed_naming_names - the UD reflexive-clitic naming frames asserted
     names (grammar only; an English parse can never produce the frame, so en installs are
     byte-identical).
  2. NAMING_STATEMENT_PREROUTE + _is_first_mention_naming_statement - a declarative turn
     asserting exactly ONE name with NO correction cue routes STATEMENT before GLiNER2,
     mirroring the source-to-goal transition preroute. TWO declines keep every real correction
     on today ladder: the REPLACIVE shape ("il mio server si chiama Apollo ... anzi, si
     chiama DevBox" - two distinct names) and ANY correction/retraction cue.
  3. NAME_FIRST_MENTION_GUARD + _first_mention_naming_correction - /retract/correct declines
     a CORRECTION whose turn is naming-edges + one-name-per-subject + NO stored bearer
     (nothing to supersede) and answers status="first_mention_naming" instead of minting.
  4. remember_facts_tool reads that status and routes the turn back through the STATEMENT
     path, where the naming chains capture owns + alias (+ the IP scalar) in one call.

En-model pins run here (en_core_web_sm); the it-model pins run in the child process
(tests/_it_capture_helper.py, SPACY_MODEL=it_core_news_sm) and skip honestly without it -
the same convention as tests/test_it_engine_uplift.py. The critic live it-stack battery
re-runs them for real.
"""
import asyncio
import os
import re
import subprocess
import sys

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.extraction.linguistics as L  # noqa: E402

_HELPER = os.path.join(os.path.dirname(__file__), "_it_capture_helper.py")


def _it_model_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("it_core_news_sm") is not None
    except Exception:  # noqa: BLE001
        return False


needs_it = pytest.mark.skipif(
    not _it_model_installed(),
    reason="it_core_news_sm not installed - the Italian parse runs only on an it stack")


def _it_names(*sentences):
    "Run licensed_naming_names per sentence in ONE child process under it_core_news_sm."
    p = subprocess.run([sys.executable, _HELPER, "names", *sentences],
                       capture_output=True, text=True, timeout=600)
    path = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else ""
    assert path and os.path.exists(path), f"helper failed: rc={p.returncode} err={p.stderr[-2000:]}"
    import json
    with open(path) as fh:
        data = json.load(fh)
    os.unlink(path)
    return [tuple(x) if x is not None else None for x in data]


def _it_frames(*sentences):
    "Run licensed_naming_frames per sentence in ONE child process (the (BEARER, NAME) pairs)."
    p = subprocess.run([sys.executable, _HELPER, "frames", *sentences],
                       capture_output=True, text=True, timeout=600)
    path = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else ""
    assert path and os.path.exists(path), f"helper failed: rc={p.returncode} err={p.stderr[-2000:]}"
    import json
    with open(path) as fh:
        data = json.load(fh)
    os.unlink(path)
    return [None if x is None else tuple(tuple(pair) for pair in x) for x in data]


class _FakeCur:
    """A cursor standing in for the entity_aliases probe: `stored` = aliases that exist."""
    def __init__(self, stored=()):
        self.stored = set(stored)
        self.probes = []

    def execute(self, sql, params=None):
        self.probes.append((sql, (params or ())[0] if params else None))

    def fetchone(self):
        return ("row",) if self.stored else None


# 1. THE DECISION TABLE (no model needed - the classifier seam, deterministic)

def _decide(monkeypatch, frames, cue=None, stored=()):
    "Drive _is_first_mention_naming_statement with scripted naming frames + cue + alias state."
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_frames", lambda _t: frames)
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (cue, "confident" if cue else None))
    monkeypatch.setattr(M, "_it_repair_or_retraction_cue", lambda _t, _d=None: False)
    return M._is_first_mention_naming_statement(
        "il mio server principale si chiama DevBox", _FakeCur(stored))


def test_first_mention_naming_classifies_statement(monkeypatch):
    """THE RED PIN: the battery sentence must classify STATEMENT (or capture owns+alias+IP in
    one call - the STATEMENT route the preroute feeds). One asserted name, an in-turn bearer,
    nothing stored, no correction cue. Before FOSS-011 this helper did not exist and the turn
    routed CORRECTION on it."""
    assert _decide(monkeypatch, (("server", "devbox"),)) is True


def test_replacive_two_name_turn_never_preroutes(monkeypatch):
    """A TRUE it naming correction ("il mio server si chiama Apollo ... anzi, si chiama DevBox")
    asserts TWO distinct names - the replacive shape stays on the correction ladder, so the
    preroute can never steal it."""
    assert _decide(monkeypatch, (("server", "apollo"), ("", "devbox"))) is False


def test_cued_turn_defers_to_the_correction_dual_gate(monkeypatch):
    """Any correction/retraction cue (marker, contrastive negation, "I meant") wins over the
    naming reading - same deference as the source-to-goal transition guard."""
    assert _decide(monkeypatch, (("server", "devbox"),), cue="CORRECTION") is False


@pytest.mark.parametrize("frames", [(), None])
def test_no_frame_or_layer_down_falls_through(monkeypatch, frames):
    """No naming frame (or the layer unavailable) -> False: GLiNER2 keeps the turn - never a
    silent drop, never a preroute without evidence."""
    assert _decide(monkeypatch, frames) is False


def test_interrogative_never_preroutes(monkeypatch):
    """A question is never a naming statement, whatever it mentions."""
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_frames", lambda _t: (("server", "devbox"),))
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(L, "is_interrogative_clause", lambda _t: True)
    assert M._is_first_mention_naming_statement(
        "Come si chiama il mio server?", _FakeCur()) is False


# 1b. THE CRITIC-ROUND-1 DECLINES (split turns: pro-drop, memory, it cue screens)

def test_prodrop_naming_never_preroutes(monkeypatch):
    """THE SPLIT-TURN DEFECT, pinned at the decision seam: a naming clause with NO in-turn
    bearer ("(Anzi,) si chiama DevBox.") names whoever the DISCOURSE supplies - the chains
    refuse to guess that referent, so the preroute must never first-mention-route one. Before
    the fix the empty-bearer frame prerouted STATEMENT and the true rename split across two
    entities."""
    assert _decide(monkeypatch, (("", "devbox"),)) is False


def test_memory_gate_stored_bearer_defers(monkeypatch):
    """First mention is a MEMORY question: a turn whose bearer is ALREADY stored is the
    ladder's rename, not a first mention. The probe is the same lookup-only entity_aliases
    read the /retract/correct guard performs."""
    assert _decide(monkeypatch, (("server", "devbox"),), stored={"server"}) is False


def test_preroute_without_a_cursor_declines(monkeypatch):
    """No DB -> the first-mention decision CANNOT be made -> False (GLiNER2 keeps the turn),
    never a guessed STATEMENT off a grammar-only read."""
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_frames", lambda _t: (("server", "devbox"),))
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(M, "_it_repair_or_retraction_cue", lambda _t, _d=None: False)
    assert M._is_first_mention_naming_statement("Il mio server si chiama DevBox.") is False


def test_it_repair_marker_declines_the_preroute(monkeypatch):
    """The migration-283 Italian repair screen: "Anzi, si chiama DevBox." must never
    first-mention-route, whatever its grammar says (the bootstrap cue seed is English-only -
    this screen is the it branch's)."""
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_frames", lambda _t: (("", "devbox"),))
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(M, "_it_repair_or_retraction_cue", lambda _t, _d=None: True)
    assert M._is_first_mention_naming_statement(
        "Anzi, si chiama DevBox.", _FakeCur()) is False


def test_it_retraction_verb_declines_the_preroute(monkeypatch):
    """The migration-283 Italian retraction screen: "dimentica che ho una gatta che si chiama
    Luna" is a plain-language FORGET (grammatically a perfect first-mention naming frame:
    gatta -> luna, one name, in-turn bearer) - the verb screen must defer it to the
    retraction ladder."""
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_frames",
                        lambda _t: (("gatta", "luna"),))
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(M, "_it_repair_or_retraction_cue", lambda _t, _d=None: True)
    assert M._is_first_mention_naming_statement(
        "dimentica che ho una gatta che si chiama Luna", _FakeCur()) is False


def test_repair_screen_helper_matches_db_seeded_phrases(monkeypatch):
    """The screen helper: substring semantics over the overlay sets; empty sets (no DSN,
    pre-migration tenant) read False and never crash."""
    import src.api.main as M
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake")
    import src.api.linguistic_cue_overlay as OV
    monkeypatch.setattr(OV, "resolve_repair_markers",
                        lambda _d: frozenset({"a dire il vero"}))
    monkeypatch.setattr(OV, "resolve_retraction_verbs",
                        lambda _d: frozenset({"dimentica"}))
    assert M._it_repair_or_retraction_cue(
        "A dire il vero il server si chiama DevBox.") is True
    assert M._it_repair_or_retraction_cue(
        "dimentica che ho una gatta") is True
    assert M._it_repair_or_retraction_cue(
        "Il mio server si chiama DevBox.") is False


# 2. EN INERTNESS (real en_core_web_sm - an English install is byte-identical)

_en = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable - the deriver no-ops")


@_en
@pytest.mark.parametrize("text", [
    "My main server is called DevBox, its IP is 10.0.1.5.",
    "my dog is called Rufus",               # fresh-domain control
    "Actually my sister's name is Thorne, not Wrenna.",
])
def test_english_parses_never_produce_a_licensed_frame(text):
    """The frame needs a UD reflexive clitic; a Penn parse cannot emit one, so the preroute
    is STRUCTURALLY inert on en installs - es/en measured behavior is untouched."""
    assert L.licensed_naming_names(text) == ()


@_en
def test_english_repair_turn_still_declines_the_preroute():
    """The en replacive repair keeps the correction ladder: cue fires (marker + contrastive
    negation) and the frame reader returns nothing - both declines, on the real parse."""
    import src.api.main as M
    assert M._is_first_mention_naming_statement(
        "Actually my sister's name is Thorne, not Wrenna.") is False


# 3. THE IT BATTERY (child process under it_core_news_sm; the critic stack runs these)

@needs_it
def test_battery_sentence_asserts_exactly_one_name():
    """THE FOSS-011 repro: ONE naming frame asserting devbox -> the decider == 1 gate fires
    STATEMENT (the classification the es/en fixtures already show)."""
    (names,) = _it_names("Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5.")
    assert names == ("devbox",), names


@needs_it
def test_true_it_naming_correction_asserts_two_names():
    """KEEP-GREEN PIN: the true it naming correction ("il mio server si chiama X ... anzi, si
    chiama Y") asserts TWO distinct names over the turn - the replacive shape - so the
    decider declines and the correction ladder (which supersedes) keeps the turn. This is
    what keeps a real correction green while the first mention goes STATEMENT."""
    (names,) = _it_names("Il mio server si chiama Apollo, anzi si chiama DevBox.")
    assert names is not None and len(set(names)) == 2, names
    assert set(names) == {"apollo", "devbox"}, names


@needs_it
def test_negated_naming_asserts_nothing():
    """Negated naming ("Mio fratello non si chiama Luca") is absence - no name asserted
    (parity with the naming chains negation gate)."""
    (names,) = _it_names("Mio fratello non si chiama Luca")
    assert names == (), names


@needs_it
def test_active_calling_is_not_a_naming_frame():
    """No reflexive clitic -> plain calling ("mia madre chiama Anna ogni giorno") - the
    clitic, not the complement label, is the frame."""
    (names,) = _it_names("Mia madre chiama Anna ogni giorno")
    assert names == (), names


@needs_it
def test_self_naming_and_fresh_domain_frames():
    """Self-naming ("Mi chiamo Marco") names the speaker; a domain nothing in the code or
    the seeds mentions ("La mia barca si chiama Stella") names the boat - grammar, not
    vocabulary."""
    n1, n2 = _it_names("Mi chiamo Marco", "La mia barca si chiama Stella")
    assert n1 == ("marco",), n1
    assert n2 == ("stella",), n2


@needs_it
def test_decider_gate_maps_battery_to_statement_on_the_it_parse():
    """The composition the live battery re-runs: the REAL it parse of the battery sentence
    satisfies the decider == 1 gate (and the anzi turn does not) - the STATEMENT route fires
    on it, the replacive correction keeps its lane."""
    battery, replacive = _it_names(
        "Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5.",
        "Il mio server si chiama Apollo, anzi si chiama DevBox.")
    assert battery is not None and len(battery) == 1, battery
    assert replacive is not None and len(set(replacive)) != 1, replacive


@needs_it
def test_split_turn_b2_prodrop_frame_carries_no_bearer():
    """THE SPLIT-TURN CORRECTION PIN (grammar half, critic round 1): b1 "Il mio server si
    chiama Apollo." carries an IN-TURN bearer (the server NP) - a legal first mention; b2
    "Anzi, si chiama DevBox." is PRO-DROP: its naming frame carries an EMPTY bearer, which is
    the decider's hard decline (a discourse-supplied referent is never first-mention-routed).
    With the fix the b2 turn falls through to GLiNER2 + the correction ladder and supersedes
    apollo -> devbox on the SAME entity; before the fix it pre-routed STATEMENT and split the
    rename across two entities. (The full b2 decline is double-gated: empty bearer AND the
    anzi repair screen.)"""
    b1, b2 = _it_frames(
        "Il mio server si chiama Apollo.",
        "Anzi, si chiama DevBox.")
    assert b1 is not None and len(b1) == 1, b1
    assert b1[0][1] == "apollo", b1
    assert b1[0][0] and "server" in b1[0][0].split(), b1
    assert b2 is not None and len(b2) == 1, b2
    assert b2[0][1] == "devbox", b2
    assert b2[0][0] == "", b2  # PRO-DROP: no in-turn referent -> the decider declines


@needs_it
def test_retraction_turn_is_grammatically_a_naming_frame():
    """THE SPLIT-TURN RETRACTION PIN (grammar half): "dimentica che ho una gatta che si
    chiama Luna" parses as a PERFECT first-mention naming frame (relative-clause naming:
    bearer = the antecedent gatta, name = luna, one name) - grammar alone CANNOT save it;
    only the retraction screen (dimentica) can. The decider half of this pin is
    test_it_retraction_verb_declines_the_preroute; end to end, the live turn must RETRACT,
    never affirmatively write also_known_as."""
    (frames,) = _it_frames(
        "dimentica che ho una gatta che si chiama Luna")
    assert frames is not None and len(frames) == 1, frames
    assert frames[0][1] == "luna", frames
    assert frames[0][0] and "gatta" in frames[0][0].split(), frames


@needs_it
def test_relative_clause_naming_bearer_is_the_antecedent():
    """The relative-clause arm's bearer is the ANTECEDENT NP ("una figlia che si chiama
    Anna" -> figlia), so the memory gate probes the thing that could already be stored."""
    (frames,) = _it_frames("Ho una figlia che si chiama Anna")
    assert frames is not None and len(frames) == 1, frames
    assert frames[0] == ("figlia", "anna"), frames


@needs_it
def test_self_naming_bearer_is_the_user():
    """1sg self-naming ("Mi chiamo Marco") binds the speaking subject: bearer "user" -
    the known referent, so the memory gate probes it like any other."""
    (frames,) = _it_frames("Mi chiamo Marco")
    assert frames is not None and len(frames) == 1, frames
    assert frames[0] == ("user", "marco"), frames


# 4. THE CORRECTION-LANE FIRST-MENTION GUARD (real en deriver + a scripted alias probe)

@_en
def test_guard_fires_on_a_first_mention_naming_turn():
    """EN battery equivalent: naming edges (owns + pref_name + ip), one name per subject,
    nothing stored -> the correction has nothing to supersede -> the guard fires."""
    import src.api.main as M
    assert M._first_mention_naming_correction(
        "My main server is called DevBox, its IP is 10.0.1.5.", _FakeCur()) is True


@_en
def test_guard_declines_when_the_bearer_is_stored():
    """A stored bearer = a plausible rename target - today correction lane owns it."""
    import src.api.main as M
    cur = _FakeCur(stored={"main server"})
    assert M._first_mention_naming_correction(
        "My main server is called DevBox, its IP is 10.0.1.5.", cur) is False


@_en
def test_guard_declines_the_cued_repair():
    """A marked repair ("Actually ..., not ...") keeps today path - the cue probe runs first."""
    import src.api.main as M
    assert M._first_mention_naming_correction(
        "Actually my sister's name is Thorne, not Wrenna.", _FakeCur()) is False


def test_endpoint_guard_declines_the_marked_it_repair(monkeypatch):
    """DEFENSE IN DEPTH (critic round 1): the endpoint guard runs the SAME migration-283 it
    screens as the preroute. A marked Italian repair/retraction turn that GLiNER2 classified
    CORRECTION must NEVER be handed back as a first mention even when nothing is stored -
    every other gate here is stubbed to 'perfect first mention' so the screen alone decides."""
    import src.api.main as M
    from src.extraction import linguistics as _L

    class _F:
        def __init__(self, s, r, o):
            self.subject, self.rel_type, self.object = s, r, o

    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(M, "_it_repair_or_retraction_cue", lambda _t, _d=None: True)
    monkeypatch.setattr(_L, "derive_sentence_facts", lambda _t, _r, **k: [
        _F("gatta", "pref_name", "luna"),
    ])
    monkeypatch.setattr(_L, "linguistics_available", lambda: True)
    assert M._first_mention_naming_correction(
        "dimentica che ho una gatta che si chiama Luna", _FakeCur()) is False


def test_guard_declines_a_two_name_replacive_turn(monkeypatch):
    """One subject asserting TWO names in one turn is the replacive shape - never a first
    mention (deriver stubbed: the decision is the guard own)."""
    import src.api.main as M
    from src.extraction import linguistics as _L

    class _F:
        def __init__(self, s, r, o):
            self.subject, self.rel_type, self.object = s, r, o

    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(_L, "derive_sentence_facts", lambda _t, _r, **k: [
        _F("server", "pref_name", "apollo"),
        _F("server", "pref_name", "devbox"),
    ])
    monkeypatch.setattr(_L, "linguistics_available", lambda: True)
    assert M._first_mention_naming_correction("anzi si chiama DevBox", _FakeCur()) is False


def test_guard_declines_without_naming_edges(monkeypatch):
    """No pref_name/also_known_as edge -> not a naming turn -> today path."""
    import src.api.main as M
    from src.extraction import linguistics as _L

    class _F:
        def __init__(self, s, r, o):
            self.subject, self.rel_type, self.object = s, r, o

    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(_L, "derive_sentence_facts", lambda _t, _r, **k: [
        _F("user", "age", "34"),
    ])
    monkeypatch.setattr(_L, "linguistics_available", lambda: True)
    assert M._first_mention_naming_correction("ho 34 anni", _FakeCur()) is False


# 5. THE MCP HANDOFF: remember_facts routes the declined correction through STATEMENT

_UID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_BATTERY = "Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5."


@pytest.fixture(autouse=True)
def _pin_ready():
    """Skip the URL-probe + provisioning gate so every request stays inside the mocks
    (tests/mcp/test_remember_facts_failsafe.py convention)."""
    import src.mcp.server as S
    S._FAULTLINE_URL_DETECTED = True
    S._provisioned_users.add(_UID)
    yield
    S._provisioned_users.discard(_UID)


def _run_handoff(monkeypatch, retract_body):
    """Drive remember_facts_tool with a CORRECTION classification whose retract POST answers
    retract_body; spy both lanes. Returns (result, retract_calls, spine_calls)."""
    import src.mcp.server as S

    async def _classify(text, user_id, **k):
        return ("CORRECTION", 0.95, 0.70)

    retract_calls = []

    async def _retract(text, user_id, **k):
        retract_calls.append(text)
        return dict(retract_body)

    spine_calls = []

    async def _spine(text, user_id, **k):
        spine_calls.append(text)
        return {"status": "stored", "committed": 3}

    async def _retention(text, user_id, intent=None, source=None):
        return "stored"

    async def _harvest(text, user_id, source=None):
        return 1

    monkeypatch.setattr(S, "_classify_and_gate", _classify)
    monkeypatch.setattr(S, "retract_fact_tool", _retract)
    monkeypatch.setattr(S, "_ingest_statement_via_spine", _spine)
    monkeypatch.setattr(S, "_episodic_capture", _retention)
    monkeypatch.setattr(S, "_harvest_turn_facts", _harvest)
    monkeypatch.setattr(S, "_passes_ingest_gate", lambda _t: True)
    monkeypatch.setattr(S, "_statement_extractor_route", lambda _u: asyncio.sleep(0, "spine"))
    result = asyncio.run(S.remember_facts_tool(_BATTERY, _UID))
    return result, retract_calls, spine_calls


def test_first_mention_handoff_routes_back_through_the_statement_path(monkeypatch):
    """THE HANDOFF PIN: the backend declined the correction (first_mention_naming - nothing
    was written) -> remember_facts falls through to the STATEMENT pipeline (spine capture:
    owns + alias + IP in one call), and does NOT return the retract body. No loop: exactly
    one classification, one retract POST, one statement ingest."""
    result, retract_calls, spine_calls = _run_handoff(
        monkeypatch, {"status": "first_mention_naming", "message": "declined"})
    assert retract_calls == [_BATTERY]
    assert spine_calls == [_BATTERY]
    assert result.get("status") == "stored", result


def test_a_real_correction_is_returned_verbatim(monkeypatch):
    """Counter-pin: a normal corrected body is surfaced as-is - the handoff triggers only on
    the guard own status, never on the correction lane answers."""
    result, retract_calls, spine_calls = _run_handoff(
        monkeypatch, {"status": "corrected", "facts_superseded": 1, "committed": 0})
    assert result.get("status") == "corrected", result
    assert spine_calls == []


def test_handoff_status_is_not_a_failure():
    """A model-invoked retract_fact that receives the handoff must not see it stamped as a
    fault - it is a routing answer (the query_detected class), and the message tells the
    model the statement path owns the turn."""
    import src.mcp.server as S
    assert S.status_is_failure("first_mention_naming") is False




# 7. CRITIC-ROUND-1 REGRESSION PINS: the is_retraction order + the migration seeds

def _correct_fact_ast():
    "Parse src.api.main and return the correct_fact AST node."
    import ast
    import src.api.main as M
    with open(M.__file__) as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "correct_fact":
            return node
    raise AssertionError("correct_fact not found in src/api/main.py")


def test_is_retraction_is_never_read_before_its_store():
    """THE ORDER PIN (critic round 1, BLOCKER): the first-mention guard read `is_retraction`
    before the variable was assigned - every POST /retract/correct 500'd with
    UnboundLocalError (ALL corrections AND retractions) while the guard flag shipped ON.
    AST-pinned the same way the critic verified it: inside correct_fact, EVERY Load of
    `is_retraction` must sit BELOW its single Store. Fails red on any future reorder that
    re-creates the unbound read."""
    import ast
    fn = _correct_fact_ast()
    store_lines = []
    load_lines = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "is_retraction":
                    store_lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "is_retraction" \
                and isinstance(node.ctx, ast.Load):
            load_lines.append(node.lineno)
    assert store_lines, "is_retraction is never assigned in correct_fact"
    if load_lines:
        assert min(load_lines) > min(store_lines), (
            f"is_retraction LOAD at line(s) {load_lines} precedes its FIRST STORE at "
            f"{min(store_lines)} - the UnboundLocalError regression")


def test_first_mention_guard_sits_below_the_is_retraction_assignment():
    """Position half of the same pin: the guard's `if NAME_FIRST_MENTION_GUARD and not
    is_retraction:` block must sit AFTER the `is_retraction = ...` assignment in the source
    (the guard narrows the CORRECTION arm only, so it belongs below Stage 1)."""
    import src.api.main as M
    with open(M.__file__) as fh:
        lines = fh.read().splitlines()
    guard_line = next(i for i, l in enumerate(lines, 1)
                      if "if NAME_FIRST_MENTION_GUARD and not is_retraction:" in l)
    store_line = next(i for i, l in enumerate(lines, 1)
                      if l.strip().startswith('is_retraction = (req.intent == "RETRACTION"'))
    assert guard_line > store_line, (guard_line, store_line)


def test_migration_283_ships_the_it_repair_and_retraction_seeds():
    """The it-branch repair/retraction screens are DB-SEEDED, not in-code (the floors
    doctrine): migration 283 must ship both categories with the members the critic's live
    repro needs - anzi (split-turn correction) and dimentica (plain-language forget) - plus
    the per-tenant back-fill loop so WARM tenants get them without reprovisioning."""
    import src.api.main as M  # noqa: F401 - repo anchored
    mig = os.path.join(os.path.dirname(M.__file__), os.pardir, os.pardir,
                       "migrations", "283_linguistic_cues_italian_repair_cues.sql")
    with open(mig) as fh:
        sql = fh.read()
    for member in ("'anzi'", "'in realtà'", "'in realta'", "'a dire il vero'",
                   "'volevo dire'", "'dimentica'", "'dimentichi'", "'cancella'",
                   "'rimuovi'"):
        assert member in sql, f"migration 283 missing cue member {member}"
    assert "repair_marker" in sql and "retraction_verb" in sql
    assert "seed_repair_marker_it" in sql and "seed_retraction_verb_it" in sql
    assert "faultline\\_%" in sql  # the Part-2 per-tenant back-fill loop
    assert "ON CONFLICT (cue, category) DO NOTHING" in sql  # idempotent


_needs_dsn = pytest.mark.skipif(
    not os.environ.get("POSTGRES_DSN"),
    reason="POSTGRES_DSN not set - the DB-seeded cue rows resolve only against a live stack")


@_needs_dsn
def test_migration_283_rows_resolve_through_the_overlay():
    """LIVE-STACK PIN: with a DSN, the overlay resolvers return the migration-283 members
    (public seed; tenant-bound reads see them after provisioning/back-fill). The preroute and
    the endpoint guard decline through these sets."""
    import src.api.linguistic_cue_overlay as OV
    dsn = os.environ["POSTGRES_DSN"]
    markers = OV.resolve_repair_markers(dsn)
    verbs = OV.resolve_retraction_verbs(dsn)
    assert "anzi" in markers, sorted(markers)[:8]
    assert "a dire il vero" in markers, sorted(markers)[:8]
    assert "dimentica" in verbs, sorted(verbs)[:8]
    assert "cancella" in verbs, sorted(verbs)[:8]


@_needs_dsn
def test_unregistered_floors_stay_empty():
    """The two new categories carry DELIBERATELY EMPTY in-code floors (the members are
    DB content): a DB-down read resolves EMPTY - never the naming-verb fallback that
    unregistered categories would borrow."""
    import src.api.linguistic_cue_overlay as OV
    assert OV._bootstrap_for(OV.REPAIR_MARKER_CATEGORY) == frozenset()
    assert OV._bootstrap_for(OV.RETRACTION_VERB_CATEGORY) == frozenset()
    assert OV.REPAIR_MARKER_CATEGORY in OV._BOOTSTRAP_BY_CATEGORY
    assert OV.RETRACTION_VERB_CATEGORY in OV._BOOTSTRAP_BY_CATEGORY




# 9. FOSS-015 PRO-DROP BEARER GROUNDING (critic round 3): the split-turn rename LANDS

class _GroundCur:
    """Scripted cursor: maps a discriminator substring of the SQL to fixed rows (or a
    callable of the params). Everything unmatched returns []."""
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self._last = []

    def execute(self, sql, params=None):
        self.calls.append(sql)
        self._last = []
        for sub, val in self.routes:
            if sub in sql:
                self._last = val(params) if callable(val) else val
                return

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


_E1, _E2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def _ground(monkeypatch, frames, routes, **kw):
    "Drive _ground_prodrop_bearer with scripted frames + a scripted cursor."
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_frames", lambda _t: frames)
    return M._ground_prodrop_bearer(_GroundCur(routes), "u1", kw.pop("text", "anzi si chiama"), **kw)


# Round-4 route discriminators (the door is keyed on entity_aliases, the label table
# this stack writes - critic round 3: zero pref_name/also_known_as FACTS rows on any it
# seat). Round 4.2: a GIVEN name arrives as user_stated (owned-definite shape, seat a's
# apollo) OR rel_default (the naming chain's scalar-identity stamp, seat b's luna) -
# both probes take the IN-pair; head nouns/L4 labels/provisioning stay excluded:
#   keys probe        SELECT DISTINCT entity_id ... WHERE alias = ANY(%s)   -> "alias = ANY"
#   holders           ... WHERE preference_source IN ('user_stated', 'rel_default')
#   held label        ... AND preference_source IN ('user_stated', 'rel_default')
#   ghost audit       SELECT entity_id, alias, ... WHERE entity_id = ANY(%s) -> "entity_id = ANY"
#   agreement         SELECT DISTINCT entity_id ... WHERE alias = %s

def test_prodrop_correction_grounds_the_unique_naming_holder(monkeypatch):
    """THE FLAGSHIP PIN: b2 "Anzi, si chiama DevBox." (pro-drop frame, bearer empty) grounds
    to the UNIQUE given-name holder - the entity holding the seat's one user_stated alias
    row (measured live: apollo/devbox/luna all land user_stated; head nouns and
    provisioning rows never do). With the bearer, the existing SCALAR
    pref_name/also_known_as lane flips apollo -> devbox ON THE SAME ENTITY (recall shows
    DevBox) instead of parking at "Old fact not found for subject change: pref_name"."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ])
    assert got and got["entity_id"] == _E1, got
    assert got["rel"] == "pref_name", got
    assert got["old_name"] == "apollo" and got["name"] == "devbox", got


def test_prodrop_correction_declines_on_two_holders(monkeypatch):
    """Two entities hold given names -> rival referents -> None (today's clarification
    path; ambiguity is never resolved by guessing)."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,), (_E2,)]),
    ])
    assert got is None


def test_agreement_gate_declines_when_old_value_not_held(monkeypatch):
    """The turn says what the bearer is called TODAY: an extracted old value that is an
    alias of a DIFFERENT entity contradicts the given-name holder -> the intersection is
    empty -> None (the agreement gate always binds)."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("WHERE alias = %s", [(_E2,)]),
    ], old_value="apollo")
    assert got is None


def test_agreement_gate_accepts_when_old_value_is_held(monkeypatch):
    """The agreeing case: old_value is an alias OF the holder -> grounding proceeds."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("WHERE alias = %s", [(_E1,)]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ], old_value="apollo")
    assert got and got["entity_id"] == _E1


def test_rival_probes_decline(monkeypatch):
    """RIVAL means the turn's OWN two references disagree: the asserted name hits one
    entity while the in-turn BEARER NP resolves to a DIFFERENT one -> None (never guess
    between them). Round 4.1 (live, seat see-dev-bug-docs): a name-content hit with NO user_stated
    row is NOT a rival - it is the MCP intent-independent harvest's own pre-correct mint
    (POST /ingest committed=0, entity + rel_default alias still written) - so that shape now
    GROUNDS to the holder (see the ghost pin); only bearer-NP rivalry still refuses here."""
    got = _ground(monkeypatch, (("server", "devbox"),), [
        ("alias = ANY", lambda p: [(_E1,)] if "devbox" in (p[0] or []) else [(_E2,)]),
    ], text="il server si chiama devbox")
    assert got is None


def test_statement_ghost_alias_hit_grounds_to_the_holder(monkeypatch):
    """THE LIVE b2 ROUTE (seat see-dev-bug-docs, 2026-10-18): the MCP's intent-independent
    harvest mints the asserted name as a standalone entity (devbox|rel_default, zero facts)
    BEFORE /retract/correct, so the door's key probe hits a GHOST. The ghost is the
    double-write's own residue, not a rival referent: it is dropped and the turn grounds to
    the unique given-name holder (apollo|user_stated) - the supersede lands on the real
    server entity instead of parking at 'Old fact not found for subject change'."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", [(_E1,)]),  # devbox -> the harvest's ghost entity
        # both E1 (ghost, rel_default) and E2 (apollo, user_stated) hold naming rows:
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,), (_E2,)]),
        # the structural audit: E1's ONLY alias is the asserted name itself
        ("entity_id = ANY", [(_E1, "devbox", False, "rel_default")]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ], old_value="old_name")  # the corrector LLM's schema placeholder, measured live
    assert got and got["entity_id"] == _E2, got
    assert got["rel"] == "pref_name" and got["old_name"] == "apollo", got


def test_placeholder_old_value_never_blocks_the_agreement(monkeypatch):
    """The corrector LLM fills the old-value slot with its SCHEMA PLACEHOLDER ('old_name')
    on every pro-drop turn (measured live: extraction old='pref_name=old_name'). The
    placeholder is not user content: the agreement gate must treat it as unstated, not
    probe alias='old_name' (always empty) and die at uniqueness."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ], old_value="old_name")
    assert got and got["entity_id"] == _E1, got


def test_no_given_name_label_declines(monkeypatch):
    """A candidate with NO user_stated alias (known only by its head noun, or a garbage
    rel_default row like the wound residue anzi|devbox) has no GIVEN name to supersede -
    that shape is a first mention (the FOSS-011 guard's territory), never a silent mint."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", [(_E1,)]),
        ("entity_id = ANY", [(_E1, "wound_alias", False, "unspecified")]),
        ("AND preference_source IN ('user_stated', 'rel_default')", []),
    ])
    assert got is None


def test_english_frames_never_open_the_door(monkeypatch):
    """No licensed naming frame (every English parse) -> None without touching the DB -
    en/es installs are byte-identical (the door is structurally it-only)."""
    cur = _GroundCur([])
    got = _ground(monkeypatch, (), [], text="actually it is called devbox")
    assert got is None
    assert cur.calls == []


def test_retraction_arm_grounds_via_alias_overlap(monkeypatch):
    """The pro-drop retraction ("dimentica che ho una gatta che si chiama Luna.")
    grounds its bearer by the turn's naming content: the forgotten name IS a stored alias of
    the gatta entity (luna|user_stated, measured live). The door hands the bearer to the
    caller, which clarifies NAMING it (the FOSS-015 doc's accepted landing) instead of the
    ENTITY lane's no-op "corrected, facts_superseded=0"."""
    got = _ground(monkeypatch, (("gatta", "luna"),), [
        ("alias = ANY", [(_E1,)]),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("luna", False)]),
    ], is_retraction=True, object_to_negate="luna",
       text="dimentica che ho una gatta che si chiama luna")
    assert got and got["entity_id"] == _E1, got
    assert got["rel"] == "also_known_as" and got["name"] == "luna", got


def test_rel_default_naming_alias_is_a_given_name(monkeypatch):
    """ROUND 4.2, THE LIVE SEAT-b SHAPE (see-dev-bug-docs, 2026-10-18): the b1 statement
    'ho una gatta che si chiama Luna.' registers the name as luna|rel_default (the
    naming chain's scalar-identity stamp - NOT user_stated; only the owned-definite shape
    reaches user_stated), so the round-4 user_stated-only gates declined the door and the
    plain-language forget fell to the ENTITY lane's no-op 'corrected, facts_superseded=0'.
    A rel_default naming alias IS a given name: the retraction grounds its bearer and the
    caller clarifies NAMING it. The bearer is kept because it co-holds its head noun
    ({gatta, luna} - the structural ghost test never fires on it)."""
    got = _ground(monkeypatch, (("gatta", "luna"),), [
        ("alias = ANY", lambda p: [(_E1,)]),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("entity_id = ANY", [(_E1, "gatta", True, "unspecified"),
                             (_E1, "luna", False, "rel_default")]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("luna", False)]),
    ], is_retraction=True, object_to_negate="luna",
       text="dimentica che ho una gatta che si chiama luna")
    assert got and got["entity_id"] == _E1, got
    assert got["rel"] == "also_known_as" and got["old_name"] == "luna", got
    # round 4.3: the clarification names THE BEARER, not the name twice
    assert got["bearer_head"] == "gatta", got


def test_overt_bearer_np_is_a_first_class_key(monkeypatch):
    """Mechanism (a): the grammar's own in-turn bearer NP ("il mio server ...") grounds the
    turn directly through the alias cache - no holder probe needed when the text names it."""
    got = _ground(monkeypatch, (("server", "devbox"),), [
        # two separate alias-ANY probes run (naming keys, then bearer NPs): route on PARAMS
        ("alias = ANY", lambda p: [(_E1,)] if "server" in (p[0] or []) else []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", []),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ], text="il mio server si chiama DevBox")
    # the bearer-key probe runs with 'server' in the params and hits E1
    assert got and got["entity_id"] == _E1, got
    assert got["bearer_np"] == "server", got


# 9b. ROUND 4 (critic round 3 root cause): alias-table keying + the speaker-flattened door

def test_holder_gate_is_keyed_on_the_alias_table_not_facts(monkeypatch):
    """THE ROUND-3 ROOT CAUSE, pinned: the door's holder gate must read entity_aliases
    (the label table it ingest writes) - never the facts table, where this stack files ZERO
    naming rows. Source-level: the helper body carries no `FROM facts` at all."""
    import inspect
    import src.api.main as M
    src_text = inspect.getsource(M._ground_prodrop_bearer)
    assert "FROM facts" not in src_text, "the door still reads the empty facts lane"
    assert "preference_source IN ('user_stated', 'rel_default')" in src_text


def test_speaker_flattened_door_requires_strict_prodrop_frames(monkeypatch):
    """Door B's grammar half: a turn that NAMES its bearer in the text ("il mio nome e
    X" / "Marco si chiama..") may legitimately BE about the speaker - the strict pro-drop
    gate (bearer-EMPTY frames) declines, and the speaker binding stands."""
    got = _ground(monkeypatch, (("server", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ], require_prodrop_frames=True,
       text="il mio server si chiama DevBox")
    assert got is None


def test_strict_prodrop_frames_still_ground(monkeypatch):
    """The same gate accepts the REAL b2 shape (bearer-empty frames) - the flattened door
    fires exactly on the pro-drop signature."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ], require_prodrop_frames=True)
    assert got and got["entity_id"] == _E1, got


def test_speaker_door_sits_between_the_ladder_and_the_dispatch():
    """Door B must run AFTER the subject ladder (it fires on the SPEAKER-bound shape)
    and BEFORE the dimension dispatch - so the SUBJECT lane (the one that dies at "Old fact
    not found for subject change: pref_name") never sees the naming shape."""
    import src.api.main as M
    with open(M.__file__) as fh:
        lines = fh.read().splitlines()
    doorb = next(i for i, l in enumerate(lines, 1)
                 if "if PRODROP_BEARER_GROUNDING and subject_uuid and subject_uuid == req.user_id:" in l)
    dispatch = next(i for i, l in enumerate(lines, 1)
                    if 'dimension = extraction.get("dimension", "RELATIONAL").upper()' in l)
    assert doorb < dispatch, (doorb, dispatch)


def test_alias_schema_invariant_the_door_reads_what_ingest_writes():
    """THE SCHEMA-INVARIANT PIN (not a scripted row in sight): the provisioning template -
    the DDL every tenant is minted from - must define the entity_aliases columns the door's
    gates query (entity_id / alias / is_preferred / preference_source). A future lane shift
    (dropping preference_source, moving labels to another table) strands the door exactly
    the way the facts-table keying did in round 3 - this pin makes that loud at test time."""
    import src.api.main as M
    tpl = os.path.join(os.path.dirname(M.__file__), os.pardir, os.pardir,
                       "src", "provisioning", "templates", "user_schema.sql")
    with open(tpl) as fh:
        ddl = fh.read()
    start = ddl.index("CREATE TABLE IF NOT EXISTS entity_aliases")
    end = ddl.index(");", start)
    block = ddl[start:end]
    for col in ("entity_id", "alias", "is_preferred", "preference_source"):
        assert re.search(rf"^\s*{col}\s+", block, re.M), (col, block[-400:])
    # and the door must key on THAT table, by name, in its holder/held gates - and read
    # the naming provenance pair the ingest write seam stamps (user_stated for the
    # owned-definite given name, rel_default for the naming chain's scalar-identity
    # stamp - round 4.2, measured live), never the empty facts-table naming rows
    import inspect
    src_text = inspect.getsource(M._ground_prodrop_bearer)
    assert src_text.count("entity_aliases") >= 3, src_text
    assert "preference_source IN ('user_stated', 'rel_default')" in src_text, src_text[-600:]


# ── ROUND 5: the door's candidates are scoped by THE TURN'S OWN CONTENT ────────────
# The round-4 holders probe was SEAT-WIDE: on a seat with >=2 named entities the
# cands-union-holders merged them and candidate_uniqueness refused EVERY pro-drop
# naming turn (the critic's seat-B reach finding: the retraction fell to the ENTITY
# lane's no-op 'corrected, facts_superseded=0'). Content binds; holders fill only the
# pure-pro-drop vacuum, ghost-stripped, and only when they are UNIQUE.

_E3 = "33333333-3333-3333-3333-333333333333"


def test_multientity_seat_turn_names_its_bearer(monkeypatch):
    """PIN (i) — THE CRITIC'S SEAT-B SHAPE: two named entities (DevBox-server E1 +
    gatta E2) on one seat; the retraction 'dimentica che ho un server che si chiama
    DevBox.' names ITS bearer through the frame's own content (the asserted name AND
    the bearer NP both hit E1). The door OPENS (grounds E1 -> the caller clarifies
    naming the bearer) instead of the ENTITY-lane no-op. RED at 05a485b8: the
    seat-wide holders union merged E1+E2 and uniqueness refused."""
    got = _ground(monkeypatch, (("server", "devbox"),), [
        ("alias = ANY", [(_E1,)]),  # devbox key AND the 'server' bearer NP both hit E1
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,), (_E2,)]),
        ("entity_id = ANY", [(_E1, "devbox", True, "user_stated"),
                             (_E1, "server", False, "inferred")]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("devbox", True)]),
    ], is_retraction=True, object_to_negate="devbox",
       text="dimentica che ho un server che si chiama devbox")
    assert got and got["entity_id"] == _E1, got
    assert got["rel"] == "pref_name" and got["old_name"] == "devbox", got
    assert got["bearer_head"] == "server", got


def test_multientity_seat_other_entity_shape_unchanged(monkeypatch):
    """PIN (ii) — the OTHER named entity's shape on the same multi-entity seat: the
    gatta/Luna retraction keeps grounding the gatta (its own content: the asserted
    name and the bearer NP both hit the gatta), byte-identical to its single-seat
    behavior."""
    got = _ground(monkeypatch, (("gatta", "luna"),), [
        ("alias = ANY", [(_E2,)]),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,), (_E2,)]),
        ("entity_id = ANY", [(_E2, "gatta", True, "unspecified"),
                             (_E2, "luna", False, "rel_default")]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("luna", False)]),
    ], is_retraction=True, object_to_negate="luna",
       text="dimentica che ho una gatta che si chiama luna")
    assert got and got["entity_id"] == _E2, got
    assert got["rel"] == "also_known_as" and got["old_name"] == "luna", got
    assert got["bearer_head"] == "gatta", got


def test_pure_prodrop_single_holder_still_grounds(monkeypatch):
    """PIN (iii) — single-holder byte-identical: the pure pro-drop turn (content hits
    NOTHING real) falls back to the seat's UNIQUE naming holder, exactly the round-4
    flagship shape and route."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,)]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("apollo", True)]),
    ], old_value="old_name")
    assert got and got["entity_id"] == _E1, got
    assert got["rel"] == "pref_name" and got["old_name"] == "apollo", got


def test_pure_prodrop_two_holders_still_shut(monkeypatch):
    """PIN (iii, negative arm) — a pure pro-drop turn on a TWO-named-entity seat
    (content hits nothing real): the holders fallback finds TWO naming holders and
    stays shut — the door never guesses across a multi-named seat."""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", []),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,), (_E2,)]),
    ], old_value="old_name")
    assert got is None


def test_inscope_ambiguity_still_refused(monkeypatch):
    """PIN (iv) — genuine IN-SCOPE ambiguity: the turn's own content matches TWO
    entities' aliases (both hold devbox). Content binds, uniqueness sees two, the door
    stays shut — clarifies without guessing. (Guard pin: green at the parent too — the
    refusal it pins is the behavior that must SURVIVE the scoping.)"""
    got = _ground(monkeypatch, (("", "devbox"),), [
        ("alias = ANY", [(_E1,), (_E2,)]),
        ("WHERE preference_source IN ('user_stated', 'rel_default')", [(_E1,), (_E2,)]),
        ("entity_id = ANY", lambda p: [(_E1, "devbox", True, "user_stated"),
                                       (_E1, "server", False, "inferred"),
                                       (_E2, "devbox", False, "rel_default"),
                                       (_E2, "gatta", True, "unspecified")]),
        ("AND preference_source IN ('user_stated', 'rel_default')", [("devbox", True)]),
    ])
    assert got is None


def test_bearer_grounding_flag_defaults_on():
    """The kill-switch ships ON (FOSS-015 is the documented fix direction); OFF = round-2
    behaviour byte-for-byte."""
    import src.api.main as M
    assert M.PRODROP_BEARER_GROUNDING is True


def test_grounding_door_sits_before_the_null_subject_clarification():
    """Position pin: the grounding door runs BEFORE the endpoint's generic null-subject
    clarification - otherwise the turn would park exactly as before the fix."""
    import src.api.main as M
    with open(M.__file__) as fh:
        lines = fh.read().splitlines()
    door = next(i for i, l in enumerate(lines, 1)
                if "if PRODROP_BEARER_GROUNDING and not subject_uuid:" in l)
    clarify = next(i for i, l in enumerate(lines, 1)
                   if "correct_fact.null_subject_extracted" in l)
    assert door < clarify, (door, clarify)


def test_correction_arm_forces_the_label_lane(monkeypatch):
    """The door's write-safety hinge, pinned at the source: the CORRECTION arm forces the
    extraction onto the SCALAR label lane (a naming clause licenses a LABEL write) - the
    LLM's SUBJECT-axis guess would move the edge to a minted entity and split the rename all
    over again. The forced assignment must sit INSIDE the door (below it, before the ladder)."""
    import src.api.main as M
    with open(M.__file__) as fh:
        lines = fh.read().splitlines()
    door = next(i for i, l in enumerate(lines, 1)
                if "if PRODROP_BEARER_GROUNDING and not subject_uuid:" in l)
    force = next(i for i, l in enumerate(lines, 1)
                 if i > door and 'extraction["dimension"] = "SCALAR"' in l)
    ladder = next(i for i, l in enumerate(lines, 1)
                  if "correct_fact.null_subject_extracted" in l)
    assert door < force < ladder, (door, force, ladder)


@needs_it
def test_split_turn_b2_frames_feed_the_grounding_door():
    """End-to-end grammar half on the REAL it parse: the b2 turn's pro-drop frame is exactly
    the door's input shape (one asserted name, empty bearer), and b1's frame carries the
    in-turn bearer - so b1 captures as a statement and b2 grounds through the holder probe."""
    b1, b2 = _it_frames(
        "Il mio server si chiama Apollo.",
        "Anzi, si chiama DevBox.")
    assert b1 and b1[0][0] and b1[0][1] == "apollo", b1
    assert b2 and b2[0] == ("", "devbox"), b2




# 9c. ROUND 4, ITEM A - the C×D-it collision rung: a naming edge whose subject is
# DISJOINT from the turn's extracted correction subject is DERIVER NOISE, never naming
# intent (the es fixtures on an en parse mint also_known_as('realidad','se mudo') while the
# correction is about DevBox - the guard's decline then misroutes a genuine correction).

class _F:
    "Minimal deriver edge (subject, rel_type, object)."
    def __init__(self, s, r, o):
        self.subject, self.rel_type, self.object = s, r, o


def _guard(monkeypatch, edges, subject_name=None, cue_hit=False, it_cue=False,
           stored=()):
    "Drive _first_mention_naming_correction with a stubbed deriver + alias probe."
    import src.api.main as M
    from src.extraction import linguistics as _L

    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(M, "_it_repair_or_retraction_cue", lambda _t, _d=None: it_cue)
    monkeypatch.setattr(_L, "derive_sentence_facts", lambda _t, _r, **k: list(edges))
    monkeypatch.setattr(_L, "linguistics_available", lambda: True)
    return M._first_mention_naming_correction(
        "turn text", _FakeCur(stored=stored), subject_name=subject_name)


def test_guard_disjoint_naming_subject_is_deriver_noise(monkeypatch):
    """THE C×D-IT PIN: a garbage naming edge (subject 'realidad') vs the turn's real
    correction subject 'devbox' - the disjoint-subject rung vetoes the decline, the
    correction lane owns the turn. Before round 4 the guard declined (status=
    first_mention_naming) and 3 lane-C pins went red on the port-worker environment."""
    assert _guard(
        monkeypatch, [_F("realidad", "also_known_as", "se mudo")],
        subject_name="DevBox") is False


def test_guard_same_subject_still_declines(monkeypatch):
    """A genuine first mention: the naming edge's subject IS the turn's subject -> the
    decline discipline stands (the rung only ever vetoes DISJOINT edges)."""
    assert _guard(
        monkeypatch, [_F("realidad", "also_known_as", "se mudo")],
        subject_name="Realidad") is True


def test_guard_disjoint_rung_ignores_first_person_subject(monkeypatch):
    """A first-person extraction subject is NOT disjoint evidence - a pro-drop naming turn
    legitimately flattens its subject to the speaker, so the decline stands (conservative)."""
    assert _guard(
        monkeypatch, [_F("realidad", "also_known_as", "se mudo")],
        subject_name="user") is True
    assert _guard(
        monkeypatch, [_F("server", "pref_name", "apollo")],
        subject_name=None) is True


def test_guard_passes_the_extracted_subject_from_the_endpoint():
    """The endpoint call site must hand the guard the extraction's subject (the rung's
    input) - a call without it silently re-opens the C×D-it wound."""
    import src.api.main as M
    with open(M.__file__) as fh:
        src_text = fh.read()
    assert ("req.text, _fm_cur, subject_name=subject_name" in src_text), \
        "the endpoint guard call lost the extraction subject"


def test_first_mention_guard_runs_after_the_extraction():
    """Position pin (round 4): the guard sits BELOW the LLM extraction - the disjoint
    rung needs the turn's real subject. A declined turn pays one extraction call; the guard
    still fires before ANY write."""
    import src.api.main as M
    with open(M.__file__) as fh:
        lines = fh.read().splitlines()
    extract = next(i for i, l in enumerate(lines, 1)
                   if "correct_fact.extraction_start" in l)
    guard = next(i for i, l in enumerate(lines, 1)
                 if "if NAME_FIRST_MENTION_GUARD and not is_retraction:" in l)
    assert extract < guard, (extract, guard)


@needs_it
def test_es_fixture_parse_still_mints_the_noise_edge():
    """The collision's grammar half, on the REAL en parse of the es fixture: the deriver
    really does emit the disjoint garbage edge (also_known_as('realidad','se mudo')) -
    documenting WHY the semantic rung exists: the edge cannot be prevented at the parse,
    only discounted at the decision."""
    names = _it_names("En realidad DevBox se mudo a 10.0.1.10 despues del cambio de red.")
    garbage = [n for n in names if n and n[0] == "realidad" and n[1] == "also_known_as"]
    assert garbage, names


# 10. FLAGS + THE EN STATEMENT-LANE FIXTURE (the parity anchor)

def test_preroute_and_guard_flags_default_on():
    """Both kill-switches ship ON (bisectable OFF = byte-for-byte legacy)."""
    import src.api.main as M
    assert M.NAMING_STATEMENT_PREROUTE is True
    assert M.NAME_FIRST_MENTION_GUARD is True


@_en
def test_en_fixture_statement_lane_captures_owns_alias_and_ip():
    """PARITY FIXTURE (en, the measured-STATEMENT branch): the statement lane the handoff
    feeds captures owns + alias + IP in ONE call - exactly the content FOSS-011 lost. The it
    battery rides the same naming chains (migrations 282), which the it-model pins above and
    the critic live battery verify."""
    import datetime
    ref = datetime.datetime(2026, 10, 17)
    facts = L.derive_sentence_facts(
        "My main server is called DevBox, its IP is 10.0.1.5.", ref) or []
    triples = {(f.subject, f.rel_type, (f.object or "").strip().lower()) for f in facts}
    assert ("user", "owns", "main server") in triples, triples
    assert ("main server", "pref_name", "devbox") in triples, triples
    assert ("main server", "ip", "10.0.1.5") in triples, triples
    assert any(getattr(f, "preferred_label", False) and f.rel_type == "pref_name"
               for f in facts), triples