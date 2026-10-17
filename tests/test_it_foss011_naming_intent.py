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
    Fraggle" is a plain-language FORGET (grammatically a perfect first-mention naming frame:
    gatta -> fraggle, one name, in-turn bearer) - the verb screen must defer it to the
    retraction ladder."""
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_frames",
                        lambda _t: (("gatta", "fraggle"),))
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(M, "_it_repair_or_retraction_cue", lambda _t, _d=None: True)
    assert M._is_first_mention_naming_statement(
        "dimentica che ho una gatta che si chiama Fraggle", _FakeCur()) is False


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
    chiama Fraggle" parses as a PERFECT first-mention naming frame (relative-clause naming:
    bearer = the antecedent gatta, name = fraggle, one name) - grammar alone CANNOT save it;
    only the retraction screen (dimentica) can. The decider half of this pin is
    test_it_retraction_verb_declines_the_preroute; end to end, the live turn must RETRACT,
    never affirmatively write also_known_as."""
    (frames,) = _it_frames(
        "dimentica che ho una gatta che si chiama Fraggle")
    assert frames is not None and len(frames) == 1, frames
    assert frames[0][1] == "fraggle", frames
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
        _F("gatta", "pref_name", "fraggle"),
    ])
    monkeypatch.setattr(_L, "linguistics_available", lambda: True)
    assert M._first_mention_naming_correction(
        "dimentica che ho una gatta che si chiama Fraggle", _FakeCur()) is False


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


# 8. FLAGS + THE EN STATEMENT-LANE FIXTURE (the parity anchor)

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