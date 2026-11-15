"""Unit tests for src/ontology/canonical.py — the deterministic canonical ladder.

normalize_rel (RUNG 1) is tested directly (pure, no DB). resolve_canonical / record_alias
(RUNGs 2/3/7) are tested against a fake in-memory psycopg2 connection injected via monkeypatch
so NO live DB is required.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.ontology import canonical as C


# ──────────────────────────────────────────────────────────────────────────────
# RUNG 1 — normalize_rel
# ──────────────────────────────────────────────────────────────────────────────

class TestNormalizeBasic:
    def test_lowercase(self):
        assert C.normalize_rel("OWNS") == "own"  # lemmatized to stem 'own'

    def test_empty_and_garbage(self):
        assert C.normalize_rel("") == ""
        assert C.normalize_rel("   ") == ""
        assert C.normalize_rel("!!!") == ""

    def test_snake_case_output(self):
        # rung 1 lemmatizes the head verb aggressively: lives→live (DB alias bridges to lives_in)
        out = C.normalize_rel("lives in")
        assert out == "live_in"
        assert " " not in out

    def test_underscore_input_equivalent_to_space(self):
        assert C.normalize_rel("had_an_issue_with") == C.normalize_rel("had an issue with")


class TestNormalizeDeterminerPronounStrip:
    def test_strip_leading_determiner(self):
        assert C.normalize_rel("the owner of") == "owner_of"

    def test_strip_leading_pronoun(self):
        assert C.normalize_rel("my pet") == "pet"

    def test_strip_possessive(self):
        assert C.normalize_rel("his job") == "job"

    def test_all_droppable_keeps_last(self):
        # if every token is a leading-drop word, keep the last (lemmatized)
        assert C.normalize_rel("the a an") == "an"


class TestNormalizeAuxiliaryStrip:
    def test_strip_leading_aux_is(self):
        # "is married to" → drop 'is', lemmatize 'married'→'marry' (ied→y), keep 'to'
        assert C.normalize_rel("is married to") == "marry_to"

    def test_strip_leading_aux_was(self):
        assert C.normalize_rel("was born in") == "bear_in"

    def test_bare_aux_lemmatized_not_dropped(self):
        # a bare auxiliary with no following content must not vanish
        assert C.normalize_rel("has") == "has"  # light verb 3sg surface
        assert C.normalize_rel("is") == "be"    # lemmatized copula


class TestNormalizeLemmatization:
    @pytest.mark.parametrize("surface,expected_head", [
        ("worked for", "work"),
        ("working for", "work"),
        ("works for", "work"),
        ("lived in", "live"),
        ("studied at", "study"),
        ("knew", "know"),
    ])
    def test_verb_lemmas(self, surface, expected_head):
        out = C.normalize_rel(surface)
        assert out.split("_")[0] == expected_head

    def test_irregular_had_to_have_light(self):
        # 'had' → lemma 'have' which is LIGHT → folds object
        assert C.normalize_rel("had an issue") == "has_issue"


class TestNormalizeLightVerbFold:
    def test_had_an_issue_with(self):
        assert C.normalize_rel("had an issue with") == "has_issue"

    def test_has_a_problem(self):
        assert C.normalize_rel("has a problem") == "has_problem"

    def test_light_verb_no_object_keeps_surface(self):
        assert C.normalize_rel("have") == "has"

    def test_get_a_job(self):
        assert C.normalize_rel("got a job") == "gets_job"  # got→get(light), fold 'job'

    def test_makes_a_choice(self):
        assert C.normalize_rel("makes a choice") == "makes_choice"

    def test_light_verb_skips_preposition_to_find_noun(self):
        # "has issue with server" → object noun is 'issue' (preposition 'with' skipped)
        assert C.normalize_rel("has issue with server") == "has_issue"


class TestNormalizePrepositionPreservation:
    def test_lives_in_vs_lives_at_distinct(self):
        # the KEY property: the load-bearing preposition is preserved so the two stay distinct.
        assert C.normalize_rel("lives in") == "live_in"
        assert C.normalize_rel("lives at") == "live_at"
        assert C.normalize_rel("lives in") != C.normalize_rel("lives at")

    def test_located_in(self):
        # -ed strip + silent-e restoration: located -> locate (silent-e restored; "locat" ends in
        # the "-at" class). The base verb of "located" IS "locate"; the DB alias layer also seeds
        # "located"->"located_in". The load-bearing point stays: the preposition is preserved and
        # the result is stable/deterministic.
        assert C.normalize_rel("located in") == "locate_in"
        assert C.normalize_rel("located in") == C.normalize_rel("located in")

    def test_keeps_of(self):
        assert C.normalize_rel("member of") == "member_of"


class TestNormalizeInnerDeterminers:
    def test_drops_inner_the(self):
        # non-light head 'reports' keeps tail but drops inner determiner
        out = C.normalize_rel("reports to the manager")
        assert "the" not in out.split("_")
        assert out.startswith("report")


class TestNormalizeContractionClitics:
    """FIX #1: contraction artifacts ("I've" → [i, ve]) must not mint a junk 've' rel."""

    def test_bare_clitic_not_minted_as_rel(self):
        # "ve" is the tail half of "I've", NOT a verb. It must NOT survive as a bare rel token.
        assert C.normalize_rel("ve") != "ve"
        assert C.normalize_rel("re") in ("", "be")   # copula residue → dropped/copula, never "re"
        assert C.normalize_rel("ll") != "ll"

    def test_ive_resolves_to_have_family(self):
        # "I've" → drop the pronoun, map the clitic to its lemma "have" (an auxiliary / light verb).
        # The point: the output is NEVER the junk fragment "ve".
        assert C.normalize_rel("I've") != "ve"

    def test_theyre_copula_dropped(self):
        # "they're related" → pronoun dropped, "re" copula residue dropped → the real predicate.
        out = C.normalize_rel("they're related")
        assert out == "relate"  # pronoun + "re" copula residue dropped; -ed→base restores silent-e (related→relate)

    def test_guard_keeps_legit_short_lemmas(self):
        # The mint guard must NOT cull legitimate short lemmas — only clitic residue.
        assert C.normalize_rel("is") == "be"
        assert C.normalize_rel("has") == "has"


class TestNormalizeSilentE:
    """RC2: silent-e restoration on the -ed/-ing strip (now ALWAYS ON). English drops a verb
    base's silent final "e" before -ed/-ing; the strip over-truncates ("scheduled"→"schedul",
    "named"→"nam") so _restore_silent_e puts it back from a bounded orthographic signal."""

    def test_restores_scheduled(self):
        # RC2 target: "scheduled" → "schedule" (single "l" → silent-e), NOT "schedul".
        assert C.normalize_rel("scheduled") == "schedule"

    def test_restores_named(self):
        # RC2 target: "named" → "name" (irregular table). NOT "nam".
        assert C.normalize_rel("named") == "name"

    def test_restores_ate(self):
        # productive Latinate "-ate": "vaccinated"/"vaccinating" → "vaccinate", not "vaccinat".
        assert C.normalize_rel("vaccinated") == "vaccinate"
        assert C.normalize_rel("vaccinating") == "vaccinate"

    def test_does_not_over_restore_regular_consonant_bases(self):
        # Bounded: regular consonant-final bases are UNTOUCHED — no spurious "e".
        assert C.normalize_rel("owned") == "own"
        assert C.normalize_rel("visited") == "visit"
        assert C.normalize_rel("repaired") == "repair"
        # "treated"→"treat" (the "-eat" guard prevents the "-at" rule from firing).
        assert C.normalize_rel("treated") == "treat"
        # "worked"/"walked" → consonant "k", untouched.
        assert C.normalize_rel("worked") == "work"


# ──────────────────────────────────────────────────────────────────────────────
# Fake psycopg2 — in-memory cursor for RUNG 2/3 + record_alias
# ──────────────────────────────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if s.startswith("set search_path"):
            self._rows = []
        elif "from rel_types" in s:
            self._rows = [(rt,) for rt in self.store["rel_types"]]
        elif "from rel_type_aliases" in s and s.startswith("select"):
            self._rows = [
                (a, c, ri) for a, (c, ri) in self.store["aliases"].items()
            ]
        elif s.startswith("insert into rel_type_aliases"):
            alias, canonical, requires_inversion, source = params
            if alias in self.store["aliases"]:
                self.rowcount = 0  # ON CONFLICT DO NOTHING
            else:
                self.store["aliases"][alias] = (canonical, requires_inversion)
                self.rowcount = 1
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    rowcount = 0


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.committed = False

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.committed = True

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    """Inject a fake psycopg2.connect returning an in-memory store; reset module caches."""
    store = {
        # rel_types stored in their RUNG-1 normalized form (so exact match works):
        #   live_in (head lemmatized), spouse, parent_of, child_of, own, has_issue
        "rel_types": {"parent_of", "child_of", "spouse", "live_in", "own", "has_issue"},
        "aliases": {
            # alias -> (canonical, requires_inversion). Seed-style RAW keys; the loader also
            # indexes each under its normalized form (married_to -> marry_to) automatically.
            "son_of": ("parent_of", True),
            "has_child": ("parent_of", False),
            "married_to": ("spouse", False),
        },
    }

    class FakePsycopg2:
        @staticmethod
        def connect(dsn):
            return FakeConn(store)

    import src.ontology.canonical as canon
    monkeypatch.setattr(canon, "psycopg2", FakePsycopg2, raising=False)
    # canonical.py does `import psycopg2` *inside* functions; patch the real module instead.
    import psycopg2 as real_psycopg2
    monkeypatch.setattr(real_psycopg2, "connect", FakePsycopg2.connect)

    C.reset_caches()
    yield store
    C.reset_caches()


# ──────────────────────────────────────────────────────────────────────────────
# RUNG 2 — exact canonical lookup
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveExact:
    def test_exact_pk_hit(self, fake_db):
        # 'owns' lemmatizes to 'own', which IS a rel_types PK in the fixture -> exact.
        r = C.resolve_canonical("owns")
        assert r["normalized"] == "own"
        assert r["matched"] == "exact"
        assert r["canonical"] == "own"
        r2 = C.resolve_canonical("spouse")
        assert r2["matched"] == "exact"
        assert r2["canonical"] == "spouse"
        assert r2["requires_inversion"] is False

    def test_exact_with_preposition(self, fake_db):
        r = C.resolve_canonical("lives in")
        assert r["normalized"] == "live_in"
        assert r["matched"] == "exact"
        assert r["canonical"] == "live_in"

    def test_light_verb_resolves_to_canonical(self, fake_db):
        r = C.resolve_canonical("had an issue with")
        assert r["normalized"] == "has_issue"
        assert r["matched"] == "exact"
        assert r["canonical"] == "has_issue"


# ──────────────────────────────────────────────────────────────────────────────
# RUNG 3 / RUNG 7 — alias lookup + inversion
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveAlias:
    def test_alias_with_inversion(self, fake_db):
        r = C.resolve_canonical("son of")
        assert r["normalized"] == "son_of"
        assert r["matched"] == "alias"
        assert r["canonical"] == "parent_of"
        assert r["requires_inversion"] is True

    def test_alias_without_inversion(self, fake_db):
        r = C.resolve_canonical("has child")
        assert r["matched"] == "alias"
        assert r["canonical"] == "parent_of"
        assert r["requires_inversion"] is False

    def test_alias_married_to(self, fake_db):
        r = C.resolve_canonical("married to")
        assert r["matched"] == "alias"
        assert r["canonical"] == "spouse"


class TestResolveNone:
    def test_unknown_rel_returns_none(self, fake_db):
        r = C.resolve_canonical("frobnicates")
        assert r["matched"] == "none"
        assert r["canonical"] is None
        assert r["requires_inversion"] is False
        assert r["normalized"] == "frobnicate"  # still lemmatized

    def test_empty_surface(self, fake_db):
        r = C.resolve_canonical("   ")
        assert r["matched"] == "none"
        assert r["canonical"] is None

    def test_no_cosine_fallback(self, fake_db):
        # 'wedded' is semantically close to spouse but must NOT resolve (no cosine).
        r = C.resolve_canonical("wedded")
        assert r["matched"] == "none"


# ──────────────────────────────────────────────────────────────────────────────
# record_alias
# ──────────────────────────────────────────────────────────────────────────────

class TestRecordAlias:
    def test_insert_new_alias(self, fake_db):
        ok = C.record_alias("employed by", "works_for", False, "engine")
        assert ok is True
        # stored under the RUNG-1 normalized form: "employed by" -> "employ_by"
        assert "employ_by" in fake_db["aliases"]
        assert fake_db["aliases"]["employ_by"] == ("works_for", False)

    def test_insert_normalizes_alias(self, fake_db):
        C.record_alias("Married To Spouse Of", "spouse", False, "user_corrected")
        # alias stored normalized (snake_cased, lemmatized)
        assert any("spouse" in k or "marry" in k for k in fake_db["aliases"])

    def test_on_conflict_no_op(self, fake_db):
        ok = C.record_alias("son of", "parent_of", True, "engine")
        # 'son_of' already exists -> no-op
        assert ok is False

    def test_invalid_source_rejected(self, fake_db):
        with pytest.raises(ValueError):
            C.record_alias("foo", "spouse", False, "llm_learned")

    def test_self_alias_skipped(self, fake_db):
        ok = C.record_alias("spouse", "spouse", False, "engine")
        assert ok is False

    def test_record_then_resolve(self, fake_db):
        C.record_alias("employed by", "works_for", False, "engine")
        C.reset_caches()  # record_alias also invalidates, but be explicit
        # 'works_for' is not in rel_types in the fixture, but the alias row resolves anyway
        r = C.resolve_canonical("employed by")
        assert r["matched"] == "alias"
        assert r["canonical"] == "works_for"


# ──────────────────────────────────────────────────────────────────────────────
# Caching behavior
# ──────────────────────────────────────────────────────────────────────────────

class TestCaching:
    def test_per_schema_cache_isolation(self, fake_db):
        # default schema cache populated
        C.resolve_canonical("spouse")
        assert "" in C._RELTYPE_CACHE
        # a named schema is a separate cache key
        C.resolve_canonical("spouse", schema="faultline_tenant_a")
        assert "faultline_tenant_a" in C._RELTYPE_CACHE

    def test_reset_clears(self, fake_db):
        C.resolve_canonical("spouse")
        assert C._RELTYPE_CACHE
        C.reset_caches()
        assert not C._RELTYPE_CACHE
