"""ABBREVIATION-DEFINITION WARRANT — the producer naming the licence for its co-reference.

WHAT IS BEING PINNED, AND WHY IT IS SPLIT THREE WAYS.

The weld guard's third arm (``unwarranted_growth_coreference``) could not be armed because ONE
production refusal in 48 was legitimate: ``{sustainable aviation fuel} ≡ {saf}``. That was never
an arm defect — the generic extraction ``also_known_as``/``pref_name`` lane emitted a genuine
abbreviation definition and an invented co-reference with the SAME provenance and no recorded
licence, and no rule downstream can separate what the producer conflated. The fix is at the
producer: run Schwartz & Hearst (2003) over the user's own sentence and, on a match, record the
``lexical`` warrant that already exists.

  * ``src/extraction/abbreviation.py`` owns the DECISION → covered behaviourally below, with
    the paper's own worked example and the four real production corruptions as controls.
  * ``src/api/main.py`` owns the WIRING → covered by AST pins, because that call site lives
    inside a several-thousand-line request handler and a scaffolded end-to-end test there tends
    to pass because the scaffolding agrees with it. The pins are written to have TEETH: they
    require the guard expression to be EXACTLY its three conjuncts, so ``if False and …`` — which
    preserves both the call text and its position while disabling the behaviour — goes red.
  * ``src/entity_registry/weld_guard.py`` owns the BAND that may be rewritten → pinned directly,
    because rewriting the wrong band is the one way this change could do harm (demoting a
    user-stated surface beneath a growth claim inverts the authority order).
"""
import ast
import os
import pathlib
import uuid

import pytest

from src.entity_registry import weld_guard
from src.entity_registry.registry import preference_is_warranted, preference_rank
from src.extraction import abbreviation

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "src" / "api" / "main.py"

# The VERBATIM production turn behind the one legitimate ARM-3 refusal, read out of the live
# tenant's episodic_log. If this stops being licensed, the arm cannot be armed.
SAF_TURN = ("[Date: 2023/05/23 (Tue) 00:34]\n"
            "I heard that some airlines are using sustainable aviation fuel (SAF). "
            "Can you tell me more about this?")


# ── THE DECISION ────────────────────────────────────────────────────────────────────────────

def test_the_one_blocking_production_refusal_is_licensed():
    """The measured blocker, pinned against the real turn it came from."""
    assert abbreviation.text_licenses_coreference(
        SAF_TURN, "sustainable aviation fuel", "saf") is True
    assert abbreviation.text_licenses_coreference(
        SAF_TURN, "saf", "sustainable aviation fuel") is True, "must be orientation-free"


@pytest.mark.parametrize("long_form,short_form,turn", [
    # Every one of these is a REAL weld measured on production and refused by ARM 3. None may
    # ever acquire a licence — a name is not an abbreviation of its own type.
    ("cat", "luna", "I'm looking for recommendations for both Luna and Max. Luna is a cat."),
    ("laptop", "cato", "all wireless clients (like my laptop, Cato's phone) connect through it"),
    ("old school friend", "emma", "I ran into Emma, an old school friend, downtown."),
    ("friend", "emily", "she reminded me of my friend Emily, who's also an aspiring actress"),
    ("film festivals", "cannes", "How does Cannes Film Festival differ from other film festivals?"),
    ("client", "hopper", "Our client, Hopper, has an issue creating ads on a global level."),
])
def test_a_name_on_its_type_node_is_never_licensed(long_form, short_form, turn):
    assert abbreviation.text_licenses_coreference(turn, long_form, short_form) is False


def test_the_papers_own_worked_example():
    """§3.2's illustration: <HSF, Heat shock transcription factor>.

    Quoted from the paper: "consider the following pair <HSF, Heat shock transcription
    factor>… although the algorithm did not match the second character correctly
    (transcription instead of shock) it still found the right long form."
    """
    assert abbreviation.find_best_long_form(
        "HSF", "Heat shock transcription factor") == "Heat shock transcription factor"


def test_the_first_character_must_match_the_start_of_a_word():
    """The paper's stated precision constraint, and the single most valuable line in the port.

    "the match of the character at the beginning of the short form must match a character in
    the initial position of the first (leftmost) word in the long form". Drop it and interior
    letters start licensing co-reference between unrelated surfaces.
    """
    # 'a','c','t' all appear IN ORDER inside these words, but 'a' never begins one.
    assert abbreviation.find_best_long_form("act", "the exact contrast") is None
    # …and the same characters DO license it once one begins a word.
    assert abbreviation.find_best_long_form("act", "a exact contrast") is not None


def test_the_discard_rule_rejects_a_long_form_containing_the_short_form():
    """"discards long forms … that include the short form as one of the words".

    Measured on 5 443 production turns: reading a bracketed word as NOT that word let four
    spurious pairs survive (``{online} == {(online) alongside …}`` and siblings).
    """
    turn = "the reserved ports (online) alongside stripe integration documents (online)"
    assert frozenset(("online", "(online) alongside stripe integration documents")) \
        not in abbreviation.licensed_pairs(turn)


@pytest.mark.parametrize("bad", [
    "a",                                   # below the two-character floor
    "abcdefghijk",                         # above the ten-character ceiling
    "one two three",                       # more than two words
    "123",                                 # no letter at all
    "-x",                                  # first character is not alphanumeric
])
def test_short_form_validity_bounds(bad):
    assert abbreviation.is_valid_short_form(bad) is False


def test_near_miss_pairings_from_the_licensing_sentence_are_all_refused():
    """DISCRIMINATION, not absence: the SAME sentence must license ONE pair and no other.

    A matcher that licensed {saf, airlines} here would be granting warrants by proximity, and
    the corpus survey would look identical from the outside.
    """
    assert abbreviation.licensed_pairs(SAF_TURN) == frozenset(
        [frozenset(("saf", "sustainable aviation fuel"))])
    for other in ("airlines", "fuel", "aviation fuel", "this", "using", "tue"):
        assert abbreviation.text_licenses_coreference(SAF_TURN, "saf", other) is False


def test_a_definition_with_no_parenthesis_yields_no_licence():
    """A STATED LIMIT, pinned so it is not mistaken for a defect later.

    "SAF stands for sustainable aviation fuel" carries a real licence this method cannot see.
    The consequence is today's behaviour (no warrant), which is the safe direction.
    """
    assert abbreviation.text_licenses_coreference(
        "SAF stands for sustainable aviation fuel.", "saf",
        "sustainable aviation fuel") is False


def test_no_text_no_licence():
    for empty in (None, "", "   "):
        assert abbreviation.licensed_pairs(empty) == frozenset()


# ── THE BAND THAT MAY BE REWRITTEN ──────────────────────────────────────────────────────────

def test_only_an_engine_growth_claim_is_rewritable():
    """The one way this change could do HARM is rewriting the wrong band.

    ``user_stated`` must never be rewritten (``lexical`` ranks BELOW it, so doing so would
    demote a surface the user actually stated beneath an engine claim — the authority order
    backwards), and neither may the provisioning floor, which records no claim to license.
    """
    assert weld_guard.is_growth_claim("user_stated") is False
    assert weld_guard.is_growth_claim("provisioned") is False
    assert weld_guard.is_growth_claim("unspecified") is False
    assert weld_guard.is_growth_claim("rel_default") is True
    assert weld_guard.is_growth_claim("inferred") is True


def test_the_warrant_buys_no_extra_trust():
    """Rank and warrant are ORTHOGONAL. If ``lexical`` ever out-ranks ``inferred`` it starts
    winning display-name contests it has not earned, which is a different feature entirely."""
    assert preference_rank("lexical") == preference_rank("inferred")
    assert preference_rank("lexical") < preference_rank("user_stated")
    assert preference_is_warranted("lexical") is True
    assert preference_is_warranted("inferred") is False


# ── THE WIRING (structural — see the module docstring for why) ──────────────────────────────

def _warrant_if_node():
    """Locate the `if` whose body assigns the WARRANTED token to ``pref_source``."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "pref_source"
                            for t in stmt.targets)
                    and isinstance(stmt.value, ast.Name)
                    and stmt.value.id == "_WARRANTED_LEXICAL_SOURCE"):
                return node
    raise AssertionError(
        "no `if …: pref_source = _WARRANTED_LEXICAL_SOURCE` in main.py — the producer no "
        "longer records the abbreviation-definition warrant, so ARM 3 is back to refusing a "
        "legitimate acronym")


def test_the_warrant_is_gated_by_exactly_its_four_conjuncts():
    """POSITION IS NOT EFFECT — this pin is written to survive a mutation run.

    A grep-shaped pin ("the call appears above the assignment") passes under
    ``if False and …``, which keeps every token and every ordering while disabling the
    behaviour outright. So this asserts the guard expression IS an ``and`` of EXACTLY four
    values and that each one is a required conjunct: drop one, add a constant, or short-circuit
    the whole thing, and the count or the shape stops matching.

    ⚠️ IT WAS THREE, AND THE FOURTH IS THE FIX FOR A MEASURED DEFECT — do not "simplify" it
    away. The rewrite used to run UNCONDITIONALLY while the design note claimed ``ARM3=off``
    was therefore inert. It is not: the rewrite DEMOTES rank 4 → rank 3, which diverges the
    registry's stored ``is_preferred`` and its cross-entity conflict staging. The arm-3 mode
    gate is what makes ``off`` inert BY NOT RUNNING. See
    ``test_at_arm3_off_the_producer_grants_no_warrant_at_all``.
    """
    node = _warrant_if_node()
    assert isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.And), (
        "the warrant must be gated by a conjunction of all four conditions")
    values = node.test.values
    assert len(values) == 4, (
        f"expected exactly 4 conjuncts, found {len(values)} — a conjunct was removed, or a "
        f"constant (e.g. `False and …`) was introduced")

    kinds = []
    for v in values:
        if isinstance(v, ast.Compare) and isinstance(v.left, ast.Call) \
                and isinstance(v.left.func, ast.Attribute) \
                and v.left.func.attr == "arm3_mode" \
                and isinstance(v.left.func.value, ast.Name) \
                and v.left.func.value.id == "weld_guard":
            assert len(v.ops) == 1 and isinstance(v.ops[0], ast.NotEq), (
                "the arm-3 gate must be `!= 'off'` — an `== 'off'` inverts it and grants the "
                "warrant ONLY when its consumer is disarmed")
            assert [c.value for c in v.comparators] == ["off"], (
                f"the arm-3 gate must compare against 'off', not {[getattr(c, 'value', c) for c in v.comparators]}")
            kinds.append("arm3_gate")
        elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                and v.func.id == "_is_growth_claim_source":
            assert [a.id for a in v.args if isinstance(a, ast.Name)] == ["pref_source"]
            kinds.append("band")
        elif isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.Not) \
                and isinstance(v.operand, ast.Call) \
                and isinstance(v.operand.func, ast.Name) \
                and v.operand.func.id == "preference_is_warranted":
            assert [a.id for a in v.operand.args if isinstance(a, ast.Name)] == ["pref_source"]
            kinds.append("already_warranted")
        elif isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) \
                and v.func.attr == "text_licenses_coreference":
            kinds.append("licence")
        else:  # pragma: no cover - only reached when the gate is mutated
            raise AssertionError(f"unrecognised conjunct in the warrant gate: {ast.dump(v)}")
    assert set(kinds) == {"arm3_gate", "band", "already_warranted", "licence"}
    assert kinds[0] == "arm3_gate", (
        "the arm-3 mode gate must come FIRST so the licence matcher is not even run — and, more "
        "importantly, so no reader can mistake it for an afterthought")


def test_the_licence_call_is_bound_to_real_values_not_constants():
    """A COLUMN NAME IS NOT A VALUE, and a call is not an effect.

    The sibling defect this repeats: a pin once asserted the string ``preference_source``
    appeared in a statement while the actual value bound there had been replaced by a
    constant, and every suite stayed green. So this checks the licensing call is fed the
    REQUEST TEXT and the EDGE's own two surfaces — replace any of them with a literal and this
    goes red.
    """
    node = _warrant_if_node()
    call = next(v for v in node.test.values
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr == "text_licenses_coreference")
    assert isinstance(call.func.value, ast.Name) and call.func.value.id == "abbreviation"
    bound = []
    for a in call.args:
        assert isinstance(a, ast.Attribute), (
            f"the licence must be decided from live values, not the literal {ast.dump(a)}")
        assert isinstance(a.value, ast.Name)
        bound.append(f"{a.value.id}.{a.attr}")
    assert bound == ["req.text", "edge.subject", "edge.object"], bound


def test_the_warrant_lands_before_both_alias_writers():
    """Both writers must see it, or the guard is defeated on one lane.

    ``/ingest`` writes this same claim TWICE — through ``EntityRegistry.register_alias`` and
    again through the post-commit raw-SQL sync, which reads its provenance out of
    ``_alias_pref_source``. The warrant is only effective if it is assigned BEFORE the
    recording into that dict (which feeds the raw sync) and before the registry call.
    """
    src = MAIN.read_text(encoding="utf-8")
    warrant = src.index("pref_source = _WARRANTED_LEXICAL_SOURCE")
    record = src.index("_alias_pref_source[(str(alias_subject).lower(),", warrant)
    register = src.index("registry.register_alias(", warrant)
    assert warrant < record < register


def test_the_warranted_token_is_actually_warranted():
    """Vocabulary drift would make every producer stamp a source that licenses nothing."""
    src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    token = next(n.value.value for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "_WARRANTED_LEXICAL_SOURCE"
                         for t in n.targets)
                 and isinstance(n.value, ast.Constant))
    assert preference_is_warranted(token) is True


# ── END TO END, AGAINST A REAL DATABASE ─────────────────────────────────────────────────────

DSN = os.environ.get("WELD_GUARD_TEST_DSN")

_DDL = """
CREATE SCHEMA {sch};
SET search_path TO {sch};
CREATE TABLE rel_types (rel_type text PRIMARY KEY, wikidata_pid text);
CREATE TABLE entity_aliases (
    entity_id text, alias text, is_preferred boolean DEFAULT false,
    preference_source text DEFAULT 'unspecified',
    PRIMARY KEY (entity_id, alias));
CREATE TABLE facts (
    subject_id text, object_id text, rel_type text,
    fact_provenance text, fact_class text,
    superseded_at timestamptz, archived_at timestamptz);
CREATE TABLE staged_facts (
    subject_id text, object_id text, rel_type text,
    provenance text, fact_class text,
    promoted_at timestamptz, deleted_at timestamptz);
INSERT INTO rel_types VALUES ('is_an_example_of','P31'), ('narrows','P279');
"""


@pytest.fixture()
def cur():
    if not DSN:
        pytest.skip("set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
    psycopg2 = pytest.importorskip("psycopg2")
    sch = "aw_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute(_DDL.format(sch=sch))
    conn.commit()
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
        yield c
    conn.rollback()
    with conn.cursor() as c:
        c.execute(f"DROP SCHEMA {sch} CASCADE")
    conn.commit()
    conn.close()


def test_the_same_weld_is_refused_unwarranted_and_admitted_warranted(cur, monkeypatch):
    """THE WHOLE POINT, on one node: the warrant is what opens the door — not a dead arm.

    Reproduces the production shape exactly — an engine-built PLACE (something is filed AT it)
    whose every label is an engine-growth claim, receiving a second engine-authored surface.
    Unwarranted it is refused; carrying the recorded licence it is admitted. Probing BOTH on the
    SAME node is what makes this a discrimination control rather than an absence of refusals.
    """
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "on")
    cur.execute("INSERT INTO entity_aliases VALUES (%s,%s,%s,%s)",
                ("place_node", "sustainable aviation fuel", False, "inferred"))
    cur.execute("INSERT INTO facts (subject_id,object_id,rel_type,fact_provenance) "
                "VALUES (%s,%s,%s,%s)",
                ("some_instance", "place_node", "is_an_example_of", "llm_inferred"))

    refused, arm = weld_guard.weld_verdict(cur, "place_node", "saf", "rel_default")
    assert refused is False and arm == weld_guard.ARM_UNWARRANTED

    allowed, arm2 = weld_guard.weld_verdict(cur, "place_node", "saf", "lexical")
    assert allowed is True and arm2 == weld_guard.ALLOW


def test_at_arm3_off_no_weld_verdict_differs_by_warrant(cur, monkeypatch):
    """NARROW CLAIM, AND THE NARROWNESS IS THE POINT: the VERDICT does not differ at ``off``.

    ⚠️ This test used to be named "the warrant is inert while arm 3 is disarmed" and its result
    was reported as proof that ``ARM3=off`` is inert, "byte-identical". IT PROVES NO SUCH THING.
    It compares ONE output — the ``(allow, arm)`` verdict — and a replay that holds one output
    identical cannot establish inertness across all outputs. Driving the REAL
    ``EntityRegistry.register_alias`` with the same two provenances diverged on 2 of 2
    scenarios, because the rewrite is a rank DEMOTION (4 → 3) that the registry acts on long
    after the guard has said ALLOW. The honest claim this test supports is exactly its name.
    See ``test_at_arm3_off_the_producer_grants_no_warrant_at_all`` for the inertness proof.
    """
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "off")
    cur.execute("INSERT INTO entity_aliases VALUES (%s,%s,%s,%s)",
                ("place_node", "sustainable aviation fuel", False, "inferred"))
    cur.execute("INSERT INTO facts (subject_id,object_id,rel_type,fact_provenance) "
                "VALUES (%s,%s,%s,%s)",
                ("some_instance", "place_node", "is_an_example_of", "llm_inferred"))
    for src in ("rel_default", "lexical"):
        allowed, arm = weld_guard.weld_verdict(cur, "place_node", "saf", src)
        assert allowed is True and arm == weld_guard.ALLOW


def test_arm3_observe_measures_without_refusing_and_leaves_arm1_enforcing(cur, monkeypatch):
    """THE MEASUREMENT PATH ARM 1 HAD AND ARM 3 DID NOT.

    ``ALIAS_WELD_GUARD=observe`` stands the WHOLE guard down, so measuring ARM 3 on production
    used to mean giving up ARM 1's shipped enforcement to do it. This pins the independent
    third mode: ARM 3 evaluates and ALLOWS (it is counting), while ARM 1 still refuses on the
    very same connection.
    """
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "observe")
    assert weld_guard.arm3_mode() == "observe"
    assert weld_guard.arm3_enabled() is False, "observe must never be reported as armed"

    # ARM 3's shape: engine-only labels on an engine-placed PLACE.
    cur.execute("INSERT INTO entity_aliases VALUES (%s,%s,%s,%s)",
                ("growth_place", "sustainable aviation fuel", False, "inferred"))
    cur.execute("INSERT INTO facts (subject_id,object_id,rel_type,fact_provenance) "
                "VALUES (%s,%s,%s,%s)",
                ("inst", "growth_place", "is_an_example_of", "llm_inferred"))
    allowed, arm = weld_guard.weld_verdict(cur, "growth_place", "saf", "rel_default")
    assert allowed is True and arm == weld_guard.ALLOW

    # ARM 1's shape, same connection: a user-stated label onto a USER-declared class.
    cur.execute("INSERT INTO entity_aliases VALUES (%s,%s,%s,%s)",
                ("user_place", "dog", False, "unspecified"))
    cur.execute("INSERT INTO facts (subject_id,object_id,rel_type,fact_provenance) "
                "VALUES (%s,%s,%s,%s)",
                ("fraggle", "user_place", "is_an_example_of", "user_stated"))
    refused, arm1 = weld_guard.weld_verdict(cur, "user_place", "fraggle", "user_stated")
    assert refused is False and arm1 == weld_guard.ARM_PLACE, (
        "observing ARM 3 must not stand ARM 1 down")


def test_an_unrecognised_arm3_value_resolves_to_off(monkeypatch):
    """Fail-safe: the direction on a typo is always 'do not refuse'."""
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "obsrve")
    assert weld_guard.arm3_mode() == "off"


# ════════════════════════════════════════════════════════════════════════════════════════════
# DEFECT REVIEW ROUND 2 — every test below exists because a specific claim was FALSE and was
# reproduced before it was fixed. Each names the claim it retires.
# ════════════════════════════════════════════════════════════════════════════════════════════

# ── DEFECT 1: the warrant licensed the exact corruption ARM 3 exists to refuse ───────────────
#
# Schwartz & Hearst constrain only the FIRST short-form character to a word boundary, so an
# abbreviation definition and a NAME parenthesised after its TYPE PHRASE are orthographically
# identical to them. Measured on the shipped matcher: 11 of these 20 were LICENSED. Every one
# is the `{cat} ≡ {luna}` shape ARM 3 was built for, and a warrant would have carried it PAST
# the arm. Zero of them occur in the production corpus, which is corpus luck, not a property.

_NAME_AFTER_TYPE_PHRASE = [
    # pets
    ("I have a golden retriever (Goldie).", "golden retriever", "goldie"),
    ("We adopted a border collie (Bo).", "border collie", "bo"),
    ("Our siamese cat (Sam) is twelve.", "siamese cat", "sam"),
    ("She keeps a pet rabbit (Pera) in the shed.", "pet rabbit", "pera"),
    ("My morkie dog (Fraggle) sleeps all day.", "morkie dog", "fraggle"),
    ("We got a corn snake (Sophia) last spring.", "corn snake", "sophia"),
    # people and nicknames — including the owner's real family-roster shape
    ("My sons Cato and Desmond (Dez) are here.", "desmond", "dez"),
    ("An old school friend (Emma) called me.", "old school friend", "emma"),
    ("I met my primary care provider (Priya) today.", "primary care provider", "priya"),
    ("His younger brother (Davy) moved out.", "younger brother", "davy"),
    # places
    ("We hiked the national park (Nala) trail.", "national park", "nala"),
    ("my grandmother old house (Moho)", "my grandmother old house", "moho"),
    ("The coastal fishing village (Coal) was quiet.", "coastal fishing village", "coal"),
    # objects
    ("I drive a station wagon (Stan).", "station wagon", "stan"),
    ("The gaming laptop (Gala) overheats.", "gaming laptop", "gala"),
    ("Bought a mountain bike (Monty) yesterday.", "mountain bike", "monty"),
    # verbatim production ARM-3 targets
    ("Our landing page (Gauntlet) is live.", "landing page", "gauntlet"),
    ("The company (Seedtenant) filed today.", "company", "seedtenant"),
    ("My laptop (Cato) died.", "laptop", "cato"),
    ("The cat (Luna) knocked it over.", "cat", "luna"),
]

_GENUINE_DEFINITIONS = [
    ("I heard that some airlines are using sustainable aviation fuel (SAF).",
     "sustainable aviation fuel", "saf"),
    ("She is a certified public accountant (CPA) now.", "certified public accountant", "cpa"),
    ("We turned on network address translation (NAT) at the edge.",
     "network address translation", "nat"),
    ("I filed with Veterans Affairs Canada (VAC) last week.", "veterans affairs canada", "vac"),
    ("The wildlife management plan (WMP) was approved.", "wildlife management plan", "wmp"),
    ("Donated to the American Cancer Society (ACS).", "american cancer society", "acs"),
    ("We deployed a content delivery network (CDN) in front of it.",
     "content delivery network", "cdn"),
]


@pytest.mark.parametrize("turn,long_form,short_form", _NAME_AFTER_TYPE_PHRASE)
def test_a_prefix_run_nickname_after_its_type_phrase_is_never_licensed(turn, long_form, short_form):
    """THE HARD LINE, at the producer — for the PREFIX-RUN family ONLY.

    ⚠️ RENAMED. This was ``test_a_name_parenthesised_after_its_type_phrase_is_never_licensed``,
    and THE NAME ITSELF ASSERTED SOMETHING FALSE: it claims the whole *name parenthesised after
    its type phrase* class is closed. It is not. Every nickname in ``_NAME_AFTER_TYPE_PHRASE``
    is PREFIX-RUN shaped (``{goldie}``, ``{bo}``, ``{sam}``…), which is the one family
    ``is_leading_letter_match`` closes — 11 of these 20 were licensed before the constraint, 0
    after, and that result is real. But swap each nickname for an INITIALISM-SHAPED one and 16 of
    these same 20 long forms are licensed again; see
    ``test_an_initialism_shaped_nickname_is_still_licensed_xfail`` directly below, which pins
    that open family. A test whose name overstates its scope is how the false "20 of 20 = class
    closed" claim survived review.

    What this test DOES pin, exactly: a nickname sharing a PREFIX RUN with one word of the type
    phrase never receives a licence to co-refer with it."""
    assert abbreviation.text_licenses_coreference(turn, long_form, short_form) is False, (
        f"{{{short_form}}} ≡ {{{long_form}}} was LICENSED — a name welded onto a type node is "
        f"the corruption this warrant must never authorise")


# ── DEFECT REVIEW ROUND 3, DEFECT 1: the class the constraint was said to close is HALF open ──
#
# `is_leading_letter_match` separates "does the short form distribute across the long form's
# word-initials", NOT "abbreviation vs name". A NAME whose letters happen to do that is admitted
# with certainty 1.0, and it is orthographically IDENTICAL to {saf} ≡ {sustainable aviation
# fuel} — the one genuine production pair the warrant exists to preserve. So there is no fix at
# this seam, and none was bolted on to make a docstring true.
#
# These are xfail(strict=True) DELIBERATELY: they encode "this is known-open and MEASURED".
# strict=True means an XPASS is a FAILURE — so if anyone ever closes this family, the suite goes
# RED and forces the coverage claims in src/extraction/abbreviation.py and
# DEV/DESIGN-alias-weld-guard.md §10.1.1 to be re-measured, instead of the gap silently healing
# and the docs staying wrong in the other direction.
#
# The bar any future closure must clear (do not weaken it to make these pass):
#   (1) {sustainable aviation fuel} ≡ {saf} still ADMITTED  — pinned by the genuine-definition
#       tests, which must stay green;
#   (2) this family REJECTED;
#   (3) measured on real production data in BOTH error directions, not just the refusal set.

_INITIALISM_SHAPED_NICKNAME = [
    # Same long forms as _NAME_AFTER_TYPE_PHRASE, nickname -> an initialism-shaped one.
    ("I have a golden retriever (GR).", "golden retriever", "gr"),
    ("We adopted a border collie (BC).", "border collie", "bc"),
    ("Our siamese cat (SC) is twelve.", "siamese cat", "sc"),
    ("She keeps a pet rabbit (PR) in the shed.", "pet rabbit", "pr"),
    ("My morkie dog (MD) sleeps all day.", "morkie dog", "md"),
    ("We got a corn snake (CS) last spring.", "corn snake", "cs"),
    ("An old school friend (OSF) called me.", "old school friend", "osf"),
    ("I met my primary care provider (PCP) today.", "primary care provider", "pcp"),
    ("His younger brother (YB) moved out.", "younger brother", "yb"),
    ("We hiked the national park (NP) trail.", "national park", "np"),
    ("my grandmother old house (MGOH)", "my grandmother old house", "mgoh"),
    ("The coastal fishing village (CFV) was quiet.", "coastal fishing village", "cfv"),
    ("I drive a station wagon (SW).", "station wagon", "sw"),
    ("The gaming laptop (GL) overheats.", "gaming laptop", "gl"),
    ("Bought a mountain bike (MB) yesterday.", "mountain bike", "mb"),
    ("Our landing page (LP) is live.", "landing page", "lp"),
]


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN-OPEN AND MEASURED: an initialism-shaped NAME is orthographically identical to a "
    "genuine acronym, so no rule over these two surfaces alone can separate them. 16 of the "
    "builder's own 20 type phrases re-enter this way — the OPEN family is LARGER than the "
    "PREFIX-RUN family the constraint closes (11). See abbreviation.py 'WHAT THIS CONSTRAINT "
    "DOES NOT CLOSE' and DEV/DESIGN-alias-weld-guard.md §10.1.1. An XPASS here means someone "
    "closed it — RE-MEASURE the coverage claims in both documents before deleting this marker."))
@pytest.mark.parametrize("turn,long_form,short_form", _INITIALISM_SHAPED_NICKNAME)
def test_an_initialism_shaped_nickname_is_still_licensed_xfail(turn, long_form, short_form):
    """PINS THE GAP: this SHOULD be refused (it is a NAME on a TYPE node) and is NOT."""
    assert abbreviation.text_licenses_coreference(turn, long_form, short_form) is False, (
        f"{{{short_form}}} ≡ {{{long_form}}} was LICENSED")


def test_the_one_word_long_form_rejection_is_arithmetic_not_recognition():
    """WHY the two survivors survive — and it is not that the rule spotted a name.

    A one-word long form has exactly ONE word-initial, so no two-character short form can align.
    Hyphenating the SAME one-word type restores the bypass, because §3.2's own boundary test
    (reused in ``_word_initial_chars``) makes each hyphen part word-initial. If this ever goes
    red, the protection stopped being word-count arithmetic and the docs must be re-measured."""
    # rejected — one word, one initial, nothing semantic about it
    assert abbreviation.is_leading_letter_match("lt", "laptop") is False
    assert abbreviation.is_leading_letter_match("ct", "cat") is False
    # the SAME kind of one-word type, hyphenated -> admitted again
    for short_form, long_form in [("gr", "golden-retriever"), ("eb", "e-bike"),
                                  ("ts", "t-shirt"), ("mil", "mother-in-law")]:
        assert abbreviation.is_leading_letter_match(short_form, long_form) is True, (
            f"{short_form}/{long_form} — the hyphen bypass is part of the measured finding")


@pytest.mark.parametrize("turn,long_form,short_form", _GENUINE_DEFINITIONS)
def test_a_genuine_initialism_definition_is_still_licensed(turn, long_form, short_form):
    """The constraint must not be bought by killing the thing the warrant exists for."""
    assert abbreviation.text_licenses_coreference(turn, long_form, short_form) is True


def test_the_discriminator_is_word_initial_alignment_not_a_word_list():
    """The mechanism, asserted directly: every short-form character must be WORD-INITIAL."""
    assert abbreviation.is_leading_letter_match("SAF", "sustainable aviation fuel") is True
    assert abbreviation.is_leading_letter_match("Goldie", "golden retriever") is False
    # skipping non-defining words is fine — it is an ordered subsequence over word initials
    assert abbreviation.is_leading_letter_match("FDA", "Food and Drug Administration") is True
    # each part of a hyphenated word is word-initial (the paper's own boundary test)
    assert abbreviation.is_leading_letter_match("XR", "X-ray") is True
    # non-alphanumerics in the short form are skipped, per §3.2
    assert abbreviation.is_leading_letter_match("S.A.F.", "sustainable aviation fuel") is True
    # order matters — this is not a bag of letters
    assert abbreviation.is_leading_letter_match("FAS", "sustainable aviation fuel") is False
    # fail-safe: nothing to align licenses nothing
    assert abbreviation.is_leading_letter_match("", "sustainable aviation fuel") is False
    assert abbreviation.is_leading_letter_match("SAF", "") is False


def test_the_constraint_is_a_strict_subset_of_what_the_paper_licenses():
    """It can only ever WITHHOLD a warrant, never grant one Schwartz & Hearst would not.

    Anything the module licenses must still satisfy the paper's own matcher. If a future edit
    makes the initialism rule a REPLACEMENT rather than a filter, this goes red.
    """
    for turn, long_form, short_form in _GENUINE_DEFINITIONS:
        for pair in abbreviation.licensed_pairs(turn):
            a, b = sorted(pair)
            short, long_ = (a, b) if len(a) <= len(b) else (b, a)
            assert abbreviation.find_best_long_form(short.upper(), long_) is not None or \
                abbreviation.find_best_long_form(short, long_) is not None, (
                    f"{pair} is licensed but does not satisfy the ported matcher")


def test_the_discard_rule_is_the_only_thing_rejecting_a_self_containing_long_form():
    """ISOLATES §3.2's discard rule from the leading-letter constraint.

    ⚠️ WHY THIS TEST EXISTS: the original discard-rule fixture ({online} vs a bracketed aside)
    stopped discriminating once leading-letter matching landed — the constraint independently
    rejects it, so the discard-rule mutation went GREEN and the rule was silently unproven. A
    masked mutation is a mutation with no teeth. This fixture passes leading-letter matching
    (s|am a|dams m|etals) so ONLY the discard rule can reject it.
    """
    turn = "We audited Sam Adams Metals (SAM) last quarter."
    assert abbreviation.is_leading_letter_match("SAM", "Sam Adams Metals") is True, (
        "fixture precondition — if this fails the test no longer isolates the discard rule")
    assert abbreviation.licensed_pairs(turn) == frozenset(), (
        "a long form that contains the short form as one of its words is not a definition of it")


def test_a_punctuated_word_is_still_that_word_for_the_discard_rule():
    """ISOLATES ``_word_key``. Same masking problem as above, same fix.

    "Sam," IS the word "sam"; reading it otherwise lets the self-containing long form survive
    the discard rule. Measured on production, that exact misreading kept 4 spurious pairs alive.
    """
    turn = "We met Sam, Adams Metals (SAM) yesterday."
    assert abbreviation.is_leading_letter_match("SAM", "Sam, Adams Metals") is True, (
        "fixture precondition — this must reach the discard rule to test it")
    assert abbreviation.licensed_pairs(turn) == frozenset()


def test_the_known_limits_document_the_false_licence_class():
    """A limits list that records ONLY missed licences reads as a safety argument while saying
    nothing about the dangerous half. That omission is what let defect 1 ship."""
    doc = abbreviation.__doc__ or ""
    assert "FALSE LICENCES" in doc
    assert "MISSED LICENCES" in doc
    # the residual must be named, not smoothed over
    assert "RESIDUAL" in doc


# ── DEFECT 6: the port's own sentence segmentation dropped real definitions ──────────────────

def test_a_semicolon_does_not_end_a_sentence():
    """`;` was in the terminator set. It split "…heat shock; transcription factor (HSF)…" and
    the definition was lost whole. A semicolon separates clauses WITHIN a sentence."""
    assert abbreviation.text_licenses_coreference(
        "Binding of the heat shock; transcription factor (HSF) matters.",
        "heat shock; transcription factor", "hsf") is True


def test_a_dotted_initialism_does_not_split_the_sentence():
    """A bare "." split inside "U.S." and amputated the long form."""
    assert abbreviation.text_licenses_coreference(
        "I work at the U.S. Digital Service (USDS).", "u.s. digital service", "usds") is True
    assert abbreviation._is_sentence_end("U.S. Digital", 1) is False   # (a) followed by alnum
    assert abbreviation._is_sentence_end("U.S. Digital", 3) is False   # (b) closes a letter-dot run
    assert abbreviation._is_sentence_end("It ended. Then", 8) is True  # an ordinary full stop


def test_a_real_sentence_boundary_still_bounds_the_long_form():
    """The segmentation fix must not let a candidate reach across a genuine sentence end.

    §3.1: "The long form candidate must appear in the same sentence as the short form."

    ⚠️ FIXTURE CHOSEN BY MUTATION, NOT BY EYE. The obvious phrasing ("…fuel. It costs a lot
    (SAF).") does NOT discriminate: the §3.1 word cap min(|A|+5, |A|*2) truncates the window
    before it can reach back, so removing the full-stop boundary altogether left that assertion
    green — a mutation with no teeth. This wording puts the earlier sentence INSIDE the cap, so
    only the boundary itself stops it, and it licenses the junk pair
    {saf} == {sustainable aviation fuel. nice} the moment the boundary is removed.
    """
    assert abbreviation.licensed_pairs(
        "We now use sustainable aviation fuel. Nice (SAF).") == frozenset()


# ── DEFECT 2: "ARM3=off is inert, proven byte-identical" was FALSE ───────────────────────────

def _evaluate_producer_gate(pref_source, text, subject, obj):
    """Evaluate main.py's ACTUAL warrant-gate expression — the source, not a reimplementation.

    The gate is lifted straight out of the parsed module and compiled, so this cannot drift
    from what ships. Mutate the gate and this changes with it.
    """
    from src.entity_registry.weld_guard import is_growth_claim as _is_growth_claim_source

    node = _warrant_if_node()
    expr = ast.Expression(body=node.test)
    ast.fix_missing_locations(expr)

    class _Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    return bool(eval(compile(expr, "<main.py warrant gate>", "eval"), {  # noqa: S307
        "weld_guard": weld_guard,
        "_is_growth_claim_source": _is_growth_claim_source,
        "preference_is_warranted": preference_is_warranted,
        "abbreviation": abbreviation,
        "pref_source": pref_source,
        "req": _Obj(text=text),
        "edge": _Obj(subject=subject, object=obj),
    }))


def test_at_arm3_off_the_producer_grants_no_warrant_at_all(monkeypatch):
    """THE INERTNESS PROOF, and it is a claim about the CODE NOT RUNNING.

    The rewrite used to run unconditionally, so `off` was inert only in the outputs anyone
    happened to compare. Now the gate's first conjunct is `arm3_mode() != "off"`, so at `off`
    nothing downstream can differ — there is no rewrite to differ about.
    """
    turn, long_form, short_form = _GENUINE_DEFINITIONS[0]
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "off")
    assert _evaluate_producer_gate("rel_default", turn, long_form, short_form) is False
    for mode in ("observe", "on"):
        monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", mode)
        assert _evaluate_producer_gate("rel_default", turn, long_form, short_form) is True, (
            f"at ARM3={mode} the warrant must be granted — otherwise the observe run measures "
            f"a lane that is not the one enforce will run")


def test_the_producer_still_refuses_to_rewrite_outside_the_growth_band(monkeypatch):
    """Unchanged by the arm-3 gate: user truth is never demoted, the floor never promoted."""
    turn, long_form, short_form = _GENUINE_DEFINITIONS[0]
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "on")
    assert _evaluate_producer_gate("user_stated", turn, long_form, short_form) is False
    assert _evaluate_producer_gate("unspecified", turn, long_form, short_form) is False
    assert _evaluate_producer_gate("lexical", turn, long_form, short_form) is False


_REGISTRY_DDL = """
CREATE SCHEMA {sch};
SET search_path TO {sch};
CREATE TABLE entities (id text PRIMARY KEY, entity_type text, node_role text);
CREATE TABLE entity_aliases (
  id serial PRIMARY KEY, entity_id text, alias text, display_form text,
  is_preferred boolean DEFAULT false, preference_source text DEFAULT 'unspecified',
  created_at timestamptz DEFAULT now(), UNIQUE (entity_id, alias));
CREATE UNIQUE INDEX idx_one_pref_{sch} ON entity_aliases (entity_id) WHERE is_preferred;
CREATE TABLE entity_name_conflicts (
  id serial PRIMARY KEY, alias text UNIQUE, entity_id_a text, entity_id_b text,
  status text, resolved_by text, resolved_at timestamptz, created_at timestamptz);
CREATE TABLE rel_types (rel_type text PRIMARY KEY, wikidata_pid text);
CREATE TABLE facts (subject_id text, object_id text, rel_type text, fact_provenance text,
                    superseded_at timestamptz, archived_at timestamptz);
CREATE TABLE staged_facts (subject_id text, object_id text, rel_type text, provenance text,
                    promoted_at timestamptz, deleted_at timestamptz);
CREATE TABLE entity_taxonomies (taxonomy_name text, member_entity_types text[]);
"""

_E1 = "11111111-1111-1111-1111-111111111111"
_E2 = "22222222-2222-2222-2222-222222222222"


def _registry_run(seed_sql, source, alias="saf", preferred=True):
    """Drive the REAL EntityRegistry.register_alias and return EVERY observable it touched."""
    psycopg2 = pytest.importorskip("psycopg2")
    from src.entity_registry.registry import EntityRegistry

    sch = "rw_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute(_REGISTRY_DDL.format(sch=sch))
    conn.autocommit = False
    try:
        with conn.cursor() as c:
            c.execute(f"SET search_path TO {sch}")
            c.execute(seed_sql)
        conn.commit()
        EntityRegistry(conn, schema_name=sch).register_alias(
            _E1, alias, is_preferred=preferred, preference_source=source)
        conn.commit()
        with conn.cursor() as c:
            c.execute(f"SET search_path TO {sch}")
            c.execute("SELECT entity_id, alias, is_preferred, preference_source "
                      "FROM entity_aliases ORDER BY entity_id, alias")
            aliases = c.fetchall()
            c.execute("SELECT alias, entity_id_a, entity_id_b, status "
                      "FROM entity_name_conflicts ORDER BY alias")
            conflicts = c.fetchall()
        return {"aliases": aliases, "conflicts": conflicts}
    finally:
        conn.rollback()
        with conn.cursor() as c:
            c.execute(f"DROP SCHEMA {sch} CASCADE")
        conn.commit()
        conn.close()


_SEED_INCUMBENT_RANK4 = f"""
INSERT INTO entities VALUES ('{_E1}','unknown',NULL);
INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source)
VALUES ('{_E1}','sustainable aviation fuel',true,'rel_default');
"""

_SEED_RIVAL_ENTITY = f"""
INSERT INTO entities VALUES ('{_E1}','unknown',NULL), ('{_E2}','unknown',NULL);
INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source)
VALUES ('{_E2}','saf',true,'inferred'),
       ('{_E1}','sustainable aviation fuel',false,'unspecified');
"""


@pytest.mark.skipif(not DSN, reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
@pytest.mark.parametrize("seed,label", [
    (_SEED_INCUMBENT_RANK4, "incumbent preferred alias at rank 4"),
    (_SEED_RIVAL_ENTITY, "same surface preferred on another entity"),
])
def test_the_warrant_rewrite_diverges_the_registry_write_not_only_the_verdict(seed, label,
                                                                              monkeypatch):
    """THE TEST THAT WOULD HAVE CAUGHT DEFECT 2 — it asserts an observable OTHER than the
    refusal set.

    This does NOT test the fix; it PINS THE HAZARD the fix exists to avoid, so that nobody can
    remove the arm-3 gate and re-derive "off is inert" from a verdict comparison. Driving the
    real ``register_alias`` with the two provenances the rewrite swaps between:
      * with an incumbent preferred alias at rank 4 the warranted write stores
        ``is_preferred=False`` — a DIFFERENT RENDERED DISPLAY NAME;
      * with a rival entity holding the surface, the cross-entity provenance override stops
        firing and a pending ``entity_name_conflicts`` row is staged instead.
    Neither is visible in ``weld_verdict``'s ``(allow, arm)``.
    """
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "off")
    without = _registry_run(seed, "rel_default")
    with_warrant = _registry_run(seed, "lexical")
    assert without != with_warrant, (
        f"[{label}] the rank demotion is no longer observable at the registry — if the ranks "
        f"were made equal, the arm-3 gate may be redundant, but PROVE it here before removing it")


@pytest.mark.skipif(not DSN, reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
def test_at_arm3_off_the_registry_write_is_identical_to_the_pre_warrant_write(monkeypatch):
    """INERTNESS, MEASURED AT THE REGISTRY. The producer emits the arrival provenance at `off`,
    so every observable ``register_alias`` touches is identical to the pre-warrant behaviour."""
    turn, long_form, short_form = _GENUINE_DEFINITIONS[0]
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "off")
    granted = _evaluate_producer_gate("rel_default", turn, long_form, short_form)
    assert granted is False
    emitted = "lexical" if granted else "rel_default"
    for seed in (_SEED_INCUMBENT_RANK4, _SEED_RIVAL_ENTITY):
        assert _registry_run(seed, emitted) == _registry_run(seed, "rel_default")


# ── DEFECT 3: the warrant is not DURABLE ────────────────────────────────────────────────────

@pytest.mark.skipif(not DSN, reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
@pytest.mark.parametrize("writes,expected,warranted", [
    (["lexical"], "lexical", True),
    (["rel_default", "lexical"], "rel_default", False),   # the warrant NEVER PERSISTS
    (["lexical", "rel_default"], "rel_default", False),   # a later re-ingest ERASES it
])
def test_the_recorded_warrant_is_not_durable_and_that_is_documented(writes, expected, warranted,
                                                                    monkeypatch):
    """PINS A KNOWN, DOCUMENTED NON-DURABILITY — do not read this as approval of it.

    The warrant axis is declared ORTHOGONAL to rank, but it is persisted in
    ``preference_source``, which ``register_alias`` ratchets BY RANK, and
    rank('lexical')=3 < rank('rel_default')=4. The failure direction is SAFE (a lost warrant
    reads as "unwarranted", so the guard refuses MORE), which is why this is pinned and
    documented rather than hot-fixed. The honest fix is a separate column merged by UNION:
    written and deliberately UNAPPLIED at migrations/244_alias_coreference_warrant.sql.

    If this goes red because a stored warrant now SURVIVES, that is the fix landing — update
    the design note (§9.6) and this test together.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    from src.entity_registry.registry import EntityRegistry

    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "off")
    sch = "dur_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute(_REGISTRY_DDL.format(sch=sch))
    conn.autocommit = False
    try:
        with conn.cursor() as c:
            c.execute(f"SET search_path TO {sch}")
            c.execute(f"INSERT INTO entities VALUES ('{_E1}','unknown',NULL)")
            c.execute(f"INSERT INTO entity_aliases (entity_id, alias, preference_source) "
                      f"VALUES ('{_E1}','sustainable aviation fuel','unspecified')")
        conn.commit()
        reg = EntityRegistry(conn, schema_name=sch)
        for src in writes:
            reg.register_alias(_E1, "saf", is_preferred=False, preference_source=src)
        conn.commit()
        with conn.cursor() as c:
            c.execute(f"SET search_path TO {sch}")
            c.execute("SELECT preference_source FROM entity_aliases WHERE alias='saf'")
            stored = c.fetchone()[0]
        assert stored == expected
        assert preference_is_warranted(stored) is warranted
    finally:
        conn.rollback()
        with conn.cursor() as c:
            c.execute(f"DROP SCHEMA {sch} CASCADE")
        conn.commit()
        conn.close()


def test_the_unapplied_warrant_migration_exists_and_says_it_is_unapplied():
    """The honest answer to defect 3 is a schema change; it is written, and it must stay OFF."""
    mig = MAIN.parents[2] / "migrations" / "244_alias_coreference_warrant.sql"
    assert mig.exists(), "the documented fix for the non-durable warrant is missing"
    text = mig.read_text(encoding="utf-8")
    assert "UNAPPLIED" in text
    assert "coreference_warrant" in text


# ── OBSERVABILITY: an observe run must measure BOTH directions ───────────────────────────────

@pytest.mark.skipif(not DSN, reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
def test_observe_counts_the_welds_a_warrant_carries_past_the_arm(cur, monkeypatch):
    """THE ARMING PATH WAS BLIND IN THE DIRECTION THAT MATTERS.

    The documented plan is: deploy → ARM3=observe on production → count and sample → flip. But
    observe counts REFUSALS, and a weld the warrant WRONGLY ADMITS is never refused, so it never
    logged. An observe run therefore came back clean on exactly the failure mode of defect 1.
    A granted warrant is now logged at the decision point, at the same level as a refusal.
    """
    events = []
    monkeypatch.setattr(weld_guard, "log_crit",
                        lambda _log, msg, **kw: events.append((msg, kw)))
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "observe")

    cur.execute("INSERT INTO entity_aliases VALUES (%s,%s,%s,%s)",
                ("place_node", "sustainable aviation fuel", False, "inferred"))
    cur.execute("INSERT INTO facts (subject_id,object_id,rel_type,fact_provenance) "
                "VALUES (%s,%s,%s,%s)",
                ("some_instance", "place_node", "is_an_example_of", "llm_inferred"))

    allowed, arm = weld_guard.weld_verdict(cur, "place_node", "saf", "lexical")
    assert allowed is True and arm == weld_guard.ALLOW, "a warrant must still ADMIT"
    assert [m for m, _ in events] == ["weld_guard.arm3_warrant_admitted"], (
        "a warranted weld past ARM 3 must be COUNTABLE — otherwise an observe run reports "
        "zero on the one bypass this arm has")

    events.clear()
    allowed, arm = weld_guard.weld_verdict(cur, "place_node", "saf", "rel_default")
    assert allowed is True, "observe never refuses"
    assert [m for m, _ in events] == ["weld_guard.arm3_would_refuse"]


@pytest.mark.skipif(not DSN, reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
def test_a_warrant_on_a_node_that_is_not_a_place_is_not_logged(cur, monkeypatch):
    """The admission log must fire at the DECISION point, not on every warranted write —
    otherwise it measures the WordNet lane's ordinary traffic instead of the bypass."""
    events = []
    monkeypatch.setattr(weld_guard, "log_crit",
                        lambda _log, msg, **kw: events.append((msg, kw)))
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "observe")
    cur.execute("INSERT INTO entity_aliases VALUES (%s,%s,%s,%s)",
                ("plain_node", "sustainable aviation fuel", False, "inferred"))
    allowed, arm = weld_guard.weld_verdict(cur, "plain_node", "saf", "lexical")
    assert allowed is True and arm == weld_guard.ALLOW
    assert events == []


# ── DEFECT 7: the docstring named the wrong default ──────────────────────────────────────────

def test_refuse_weld_does_not_claim_observe_is_the_default():
    """`_DEFAULT_MODE` has been "enforce" since the rollout; the docstring still said observe,
    which tells a reader a refusal here cannot skip a write. It can."""
    assert weld_guard._DEFAULT_MODE == "enforce"
    doc = weld_guard.refuse_weld.__doc__ or ""
    assert "observe`` mode (the default)" not in doc and "observe mode (the default)" not in doc


# ════════════════════════════════════════════════════════════════════════════════════════════
# DEFECT REVIEW ROUND 3 — recall honesty and citation completeness.
# ════════════════════════════════════════════════════════════════════════════════════════════

# ── DEFECT 5: recall was reported as "7/7 (100 %)", which is a PRECISION figure ───────────────
#
# "Of the pairs emitted, all were genuine" says nothing about the definitions never found, and
# the genuine-definition set it was paired with was chosen by the same author as the matcher.
# Measured on 12 must-admit definitions chosen INDEPENDENTLY: 8/12 (~67 %). The four misses are
# pinned below BY MECHANISM so that a future change that fixes or worsens one is visible.
# All four fail SAFE (no licence granted -> the alias keeps today's unwarranted provenance).

def test_recall_miss_repeated_letter_truncation_rrsp():
    """MECHANISM 1 — the paper's backward walk matches a repeated letter INTERNALLY first.

    `rrsp` matches the inner `r` of "retirement", so the span returned is the TRUNCATED
    "retirement savings plan", which has three word-initials (r,s,p) for four characters."""
    assert abbreviation.find_best_long_form(
        "rrsp", "registered retirement savings plan") == "retirement savings plan"
    assert abbreviation.text_licenses_coreference(
        "I maxed out my registered retirement savings plan (RRSP) this year.",
        "registered retirement savings plan", "rrsp") is False


def test_recall_miss_three_word_short_form_u_of_g():
    """MECHANISM 2 — a THREE-word short form is rejected by `_MAX_SHORT_WORDS = 2`.

    That cap is FAITHFUL to S&H §3.1 ("at most two words"), so this is the paper's limit
    correctly implemented — a real recall cost, not a port defect. Widening it would grant MORE
    licences, which is the unsafe direction."""
    assert abbreviation.is_valid_short_form("U of T") is False
    assert abbreviation.text_licenses_coreference(
        "I studied at the University of Waterloo (U of T).",
        "university of waterloo", "U of T") is False


def test_recall_miss_truncated_long_form_is_licensed_instead_ebs():
    """MECHANISM 3 — the dangerous one: it does not merely miss, it licenses a DIFFERENT pair.

    The hyphen makes `e` and `b` word-initial, so `ebs` aligns without ever reaching "oracle".
    The warrant is recorded for a span the user did not define, and the edge's REAL surfaces get
    nothing. Both halves are asserted, because only asserting the miss would hide the false
    licence sitting beside it."""
    turn = "We migrated Oracle E-Business Suite (EBS) to RHEL 8."
    # the pair the sentence actually defines: NO warrant
    assert abbreviation.text_licenses_coreference(turn, "oracle e-business suite", "ebs") is False
    # the truncated pair: warranted instead
    assert abbreviation.text_licenses_coreference(turn, "e-business suite", "ebs") is True


def test_recall_miss_non_initial_acronym_nsaid():
    """MECHANISM 4 — the DISCLOSED non-initial trade, shown to be non-hypothetical.

    NSAID takes its `s` from "non**S**teroidal", an INTERIOR letter — the same shape as the
    paper's own "Gcn5-related N-acetyltransferase (GNAT)" counterexample. This is the deliberate
    precision-for-recall trade working as designed, on an extremely common real acronym."""
    assert abbreviation._word_initial_chars(
        "nonsteroidal anti-inflammatory drug") == ["n", "a", "i", "d"]
    assert abbreviation.text_licenses_coreference(
        "I take a nonsteroidal anti-inflammatory drug (NSAID).",
        "nonsteroidal anti-inflammatory drug", "nsaid") is False


def test_independently_chosen_recall_is_eight_of_twelve():
    """The headline number itself, pinned so it cannot silently drift back to a rosier one."""
    cases = [
        ("I maxed out my registered retirement savings plan (RRSP) this year.",
         "registered retirement savings plan", "rrsp"),
        ("I studied at the University of Waterloo (U of T).", "university of waterloo", "U of T"),
        ("We migrated Oracle E-Business Suite (EBS) to RHEL 8.", "oracle e-business suite", "ebs"),
        ("The request for proposal (RFP) closes Friday.", "request for proposal", "rfp"),
        ("He has chronic obstructive pulmonary disease (COPD).",
         "chronic obstructive pulmonary disease", "copd"),
        ("We use a virtual private network (VPN) at work.", "virtual private network", "vpn"),
        ("I filed the goods and services tax (GST) return.", "goods and services tax", "gst"),
        ("The artificial intelligence (AI) team shipped it.", "artificial intelligence", "ai"),
        ("She joined the Royal Canadian Mounted Police (RCMP).",
         "royal canadian mounted police", "rcmp"),
        ("Our service level agreement (SLA) says four hours.", "service level agreement", "sla"),
        ("I take a nonsteroidal anti-inflammatory drug (NSAID).",
         "nonsteroidal anti-inflammatory drug", "nsaid"),
        ("The board approved the request for information (RFI).", "request for information", "rfi"),
    ]
    admitted = [c for c in cases if abbreviation.text_licenses_coreference(*c)]
    assert len(admitted) == 8, (
        f"independently-measured recall changed: {len(admitted)}/12. Re-measure and update "
        f"BOTH src/extraction/abbreviation.py (CALIBRATION) and "
        f"DEV/DESIGN-alias-weld-guard.md §10.1 row (c'') — do not just edit this number.")


# ── DEFECT 4: Larkey scheme 2 was quoted by omission while claiming verbatim fidelity ─────────

def test_leading_letter_match_is_only_larkey_sentence_one():
    """The function implements sentence 1 of "Lowercase strict", NOT the whole scheme.

    Sentence 2 — "The expansion must begin with the first letter of the acronym and must not
    contain uppercase letters" — is NOT enforced here, so STANDALONE the function is LOOSER than
    the scheme an earlier docstring said it matched "character for character". These four are
    all True and Larkey's sentence 2 rejects all four."""
    for short_form, long_form in [("saf", "the sustainable aviation fuel"),
                                  ("gr", "my big golden retriever"),
                                  ("cpa", "a certified public accountant"),
                                  ("nat", "we use network address translation")]:
        assert abbreviation.is_leading_letter_match(short_form, long_form) is True


def test_the_pipeline_satisfies_larkey_sentence_one_begins_with_clause():
    """Why the omission is a CITATION defect and not a pipeline behaviour defect.

    S&H's own `find_best_long_form` returns the span from the start of the word holding the FIRST
    matched character, so every pair `licensed_pairs` emits already begins with the acronym's
    first letter — the clause the function itself does not enforce."""
    pairs = abbreviation.licensed_pairs("We use my big golden retriever (GR) daily.")
    assert pairs == frozenset({frozenset({"golden retriever", "gr"})}), (
        "the emitted span must be trimmed to begin at the acronym's first letter, not the "
        "wider 'my big golden retriever'")


# ── DOC-CONSISTENCY PINS: each retires a specific FALSE claim that shipped ────────────────────
#
# These follow the existing `test_refuse_weld_does_not_claim_observe_is_the_default` pattern.
# Every assertion below FAILS against the pre-fix text, which is the point: a documentation
# defect that no test can see is a documentation defect that comes back.

def test_module_docstring_does_not_claim_the_name_class_is_closed():
    """DEFECT 1. The docstring called the initialism-shaped family a rare RESIDUAL, "far rarer
    than the prefix-run family this constraint kills". Measured, it is the LARGER half (16 vs
    11 on the builder's own list). Both the false comparison and the class-closed reading of
    "20 of 20" must stay gone."""
    doc = abbreviation.__doc__ or ""
    assert "far rarer than the prefix-run family" not in doc
    assert "It is now refused — 20 of 20 constructed cases" not in doc
    # …and the correction must actually be present, not merely the false claim deleted.
    assert "WHAT THIS CONSTRAINT DOES **NOT** CLOSE" in doc
    assert "FULLY OPEN" in doc


def test_error_direction_does_not_call_a_false_warrant_a_no_op():
    """DEFECT 2. Both this module and the main.py call site summarised a wrongly-granted licence
    as "which is also exactly today's behaviour", five lines above main.py's own finding that
    the rewrite is a rank DEMOTION (4->3) with two measured divergences."""
    doc = abbreviation.__doc__ or ""
    # NOTE: match the ORIGINAL CLAIM's sentence form, not the bare phrase — the corrected text
    # deliberately QUOTES the retired wording in order to name it, and a bare-phrase assertion
    # trips on the correction itself. Pin what was asserted, not what is discussed.
    assert "co-reference through, which is also exactly today's behaviour" not in doc
    assert "TRUST DEMOTION" in doc
    main_src = MAIN.read_text(encoding="utf-8")
    # Again: pin the ORIGINAL CLAIM's sentence form. The corrected comment quotes the retired
    # wording verbatim under "USED TO READ", so a bare-phrase assertion would fail on the fix.
    # The original joined the two halves with "lands today; a WRONGLY GRANTED one lets…".
    assert "lands today; a WRONGLY GRANTED" not in main_src, (
        "main.py's ERROR DIRECTION still contradicts the gate finding beneath it")
    assert "rank DEMOTION" in main_src, "the correction is not present at the call site"


def test_larkey_scheme_two_is_quoted_in_full():
    """DEFECT 4. The module claimed scheme 2 was reproduced "character for character" while
    quoting only its first sentence. The omitted sentence must now be present verbatim, and the
    verbatim-fidelity claim must be gone."""
    doc = abbreviation.__doc__ or ""
    assert "The expansion must begin with the first letter" in doc, "sentence 2 still omitted"
    assert "must not contain uppercase letters" in doc
    assert "scheme 2 IS this constraint,\n  character for character" not in doc
    assert "character for character" not in (abbreviation.is_leading_letter_match.__doc__ or "")


def test_calibration_reports_measured_recall_not_only_precision():
    """DEFECT 5. "7/7 (100 %)" is a PRECISION figure and was being read as completeness."""
    doc = abbreviation.__doc__ or ""
    assert "8 of 12 are licensed" in doc or "8/12" in doc, "independent recall not reported"
    assert "RECALL IS **NOT** 100%" in doc


def test_migration_243_fans_out_per_tenant_and_never_targets_public():
    """DEFECT 3. The header claimed "this runs against EVERY `faultline_*` schema, not `public`"
    above a BARE unqualified `ALTER TABLE entity_aliases`. Run with the default search_path that
    alters the LEGACY public table (which EXISTS on production) and ZERO tenants."""
    sql = (ROOT / "migrations" / "244_alias_coreference_warrant.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    # the fanout must be real
    assert "information_schema.schemata" in body
    assert "faultline" in body and "EXECUTE format(" in body
    # …and the bare unqualified ALTER must be gone
    assert "\nALTER TABLE entity_aliases" not in body, (
        "unqualified ALTER would hit public.entity_aliases and zero tenants")
    # idempotent, per the repo convention
    assert "ADD COLUMN IF NOT EXISTS" in body and "CREATE INDEX IF NOT EXISTS" in body
    # the user_id hazard: a per-tenant predicate on a column these tables do not have aborts
    # the caller's transaction (CLAUDE.md, "Database Schema").
    assert "user_id" not in body
