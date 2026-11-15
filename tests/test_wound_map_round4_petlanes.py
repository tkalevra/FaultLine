"""THE WOUND-MAP ROUND-4 PET-LANE PINS (2026-11-13) — the two capture wounds the round-4
critic measured live on the door (deterministic across two fresh-seat runs):

WOUND 1 — PET3 TYPE_REFUSED / the cecil class. "…our bearded dragon Cecil lives in the
40-gallon tank by the window." landed only (cecil, instance_of, bearded dragon) + a stray
(user, owns, cecil); the pets walk's own (user, has_pet, cecil) edge NEVER EXISTED TO
ADMIT: 'bearded dragon' is outside every pet-noun class and every typing signal, so
`_possession_rel_for_type` resolved the GENERIC default owns. FIX (SPINE_PET_REL_SUPERSENSE,
default ON, OFF = byte-parity): the WordNet supersense ladder — the same metadata rail the
#38 guard reads — supplies the class signal ('dragon' definitely matches Animal), and the
narrowest-tail pick (has_pet={Animal} over owns={Animal,Object,…}) follows. Plus the
ADMISSION-side evidence mirror in main.py (PET_TYPE_FROM_LANE_CONTEXT, default ON, OFF =
byte-parity): a refused possession-family edge whose endpoint holds a live pet-lane
instance_of edge (a named instance of an animal-grounded kind) falls the endpoint's type to
Animal, re-validates, admits on pass — the KIN_TYPE_FROM_LANE_CONTEXT pattern.

WOUND 2 — the biscuit-breed mis-bind. "Our dog Biscuit is a golden retriever — we adopted
him on April 10, 2023." landed the breed ONLY as the USER attr (user, dog, 'golden
retriever') live; the deterministic divergence vs the corgi shape is the CAPITALIZED breed
orthography (the standard way users write breeds): "Golden Retriever" parses as a PROPN
compound, the NOUN-only type scan in analyze_name_type_bindings never sees it, no
copula_type binding mints, and the (breed, also_known_as, biscuit) rung the breed query
walks never exists. FIX (COPULA_PROPN_KIND_BIND, default ON, OFF = byte-parity): a
PROPN-headed copula attr binds as the kind phrase ONLY on same-doc evidence — the copula
subject's appositive name must already be bound to a type by another binding (the "our dog
Biscuit" appos). The lowercase 'golden retriever' atom derives the full ladder at every
deterministic layer (pinned here so a union-side loss can never mask a derive loss).

Hermetic: no DB, no LLM, no GLiNER.
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.extraction.linguistics as L  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (REPO / "src" / "api" / "main.py").read_text(encoding="utf-8")
LING_SOURCE = (REPO / "src" / "extraction" / "linguistics.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable (en_core_web_sm not installed) — "
           "spine deriver no-ops; executes wherever the model loads",
)

_REF = datetime.datetime(2026, 11, 13)

PET3_TURN = ("Nutmeg's vet appointment is on the 22nd at 9:30am at Cedar Grove "
             "Veterinary Clinic, and our bearded dragon Cecil lives in the 40-gallon "
             "tank by the window.")
CECIL_ATOM = "Our bearded dragon Cecil lives in the 40-gallon tank by the window."
BREED_TURN = "Our dog Biscuit is a golden retriever — we adopted him on April 10, 2023."


def _derive(text):
    doc = L._parse(text)
    return {(f.subject, f.rel_type, str(f.object))
            for f in (L.derive_sentence_facts(doc, _REF) or [])}


# ── WOUND 1: the cecil pet-rel resolution ───────────────────────────────────────────

def test_w1_cecil_atom_mints_the_pet_edge():
    """The captured atom 2 (verbatim from the live brain) mints the pets walk's OWN edge:
    (user, has_pet, cecil) beside the instance_of rung — not the stray owns."""
    triples = _derive(CECIL_ATOM)
    assert ("user", "has_pet", "cecil") in triples
    assert ("cecil", "instance_of", "bearded dragon") in triples
    assert ("user", "owns", "cecil") not in triples


def test_w1_full_pet3_turn_mints_the_pet_edge():
    """The compound turn (both clauses, post-ENUM_PRESPLIT) carries the has_pet rung too."""
    triples = _derive(PET3_TURN)
    assert ("user", "has_pet", "cecil") in triples
    assert ("cecil", "instance_of", "bearded dragon") in triples


def test_w1_resolver_picks_has_pet_for_animal_grounding():
    """The supersense leg: a definite Animal match on the type head resolves the pet rel
    (narrowest tail), an object head keeps owns, and a tri-state None keeps owns."""
    assert L._possession_rel_for_type("dragon") == "has_pet"
    assert L._possession_rel_for_type("bearded dragon") == "has_pet"
    assert L._possession_rel_for_type("genet") == "has_pet"
    assert L._possession_rel_for_type("server") == "owns"
    assert L._possession_rel_for_type("cottage") == "owns"
    # tri-state safe: a nonsense word (ladder returns None) keeps the generic default
    assert L._possession_rel_for_type("zzqxj") == "owns"


def test_w1_flag_off_is_byte_parity_with_the_owns_default():
    """SPINE_PET_REL_SUPERSENSE=OFF → the unseeded species resolves the GENERIC default
    exactly as the live capture showed (the red-state receipt: the stray owns)."""
    old = L.SPINE_PET_REL_SUPERSENSE
    L.SPINE_PET_REL_SUPERSENSE = False
    try:
        assert L._possession_rel_for_type("dragon") == "owns"
        triples = _derive(CECIL_ATOM)
        assert ("user", "owns", "cecil") in triples
        assert ("user", "has_pet", "cecil") not in triples
    finally:
        L.SPINE_PET_REL_SUPERSENSE = old


def test_w1_non_pet_possessives_never_mint_pet_edges():
    """The negative guard: a non-pet 'our X' possessive (an object head, a place head)
    keeps the owns reading — the supersense leg never fires off the Animal class."""
    for turn, wrong in (("Our server Apollo lives in the rack.", "has_pet"),
                        ("Our cottage by the lake is rented.", "has_pet")):
        triples = _derive(turn)
        assert not any(r == wrong for _, r, _o in triples), turn
        assert ("user", "owns", "server") in triples or \
            ("user", "owns", "cottage by lake") in triples, turn


def test_w1_corgi_control_shape_unchanged():
    """The seeded pet shape keeps its exact ladder (no behavior change where the class
    already admitted the head)."""
    triples = _derive("Our dog Biscuit is a corgi.")
    assert ("user", "has_pet", "biscuit") in triples
    assert ("biscuit", "instance_of", "dog") in triples
    assert ("corgi", "also_known_as", "biscuit") in triples


# ── WOUND 1: the admission-side evidence mirror (main.py) ───────────────────────────

def test_w1_pet_lane_evidence_arm_is_wired():
    """Structural: the flag, the evidence helper, and the admission arm exist, the arm is
    gated on the flag FIRST (OFF = the refusal stands byte-for-byte), and the guard chain
    is the possession family + any role + a case-insensitive Animal allowed-check."""
    assert "_PET_TYPE_FROM_LANE_CONTEXT = (" in MAIN_SOURCE
    assert 'os.environ.get("PET_TYPE_FROM_LANE_CONTEXT", "true")' in MAIN_SOURCE
    assert "def _entity_pet_instance_evidence(" in MAIN_SOURCE
    assert ("if status == TYPE_REFUSED_STATUS and _PET_TYPE_FROM_LANE_CONTEXT"
            in MAIN_SOURCE)
    assert "ingest.pet_type_from_lane_evidence" in MAIN_SOURCE
    # the evidence leg is load-bearing: the re-type runs only after _entity_pet_instance_evidence
    ev_pos = MAIN_SOURCE.index("_pet_ev = _entity_pet_instance_evidence(db, _pet_ev_off)")
    upd_pos = MAIN_SOURCE.index("UPDATE entities SET entity_type = 'Animal'",
                                MAIN_SOURCE.index("PET-LANE TYPE FALLBACK, THE EVIDENCE MIRROR"))
    assert ev_pos < upd_pos, "the re-type must sit behind the live-evidence check"
    # case-insensitive Animal allowed-check (a tenant 'ANIMAL' row must not dodge the arm)
    assert 'str(_a or "").strip().lower() == "animal"' in MAIN_SOURCE


def test_w1_pet_instance_evidence_helper_is_fail_safe():
    """No db / no entity → False (never fabricate a typing); the supersense rail is the
    helper's own vocabulary (a kind stored Animal OR an alias the ladder grounds)."""
    from src.api.main import _entity_pet_instance_evidence
    assert _entity_pet_instance_evidence(None, "x") is False
    assert _entity_pet_instance_evidence(None, None) is False
    assert "supersense_type_match" in MAIN_SOURCE[MAIN_SOURCE.index(
        "def _entity_pet_instance_evidence"):MAIN_SOURCE.index(
        "def _entity_pet_instance_evidence") + 3000]


def test_w1_kin_arm_untouched():
    """Byte-parity discipline: the kin arm's own guard chain is unchanged beside the new
    pet mirror (both arms keep their honest markers)."""
    assert "ingest.kin_type_from_lane_context" in MAIN_SOURCE
    assert '_KIN_TYPE_FROM_LANE_CONTEXT = (' in MAIN_SOURCE


# ── WOUND 2: the biscuit-breed binding ──────────────────────────────────────────────

def test_w2_capitalized_breed_mints_the_copula_kind_binding():
    """'Our dog Biscuit is a Golden Retriever.' — the PROPN-compound attr binds as the
    kind phrase on the same-doc appos evidence (the corgi shape's copula_type twin)."""
    binds = L.analyze_name_type_bindings(L._parse("Our dog Biscuit is a Golden Retriever."))
    shapes = {(b.name.lower(), b.type_noun.lower(), b.connector) for b in binds}
    assert ("biscuit", "dog", "appos") in shapes
    assert ("biscuit", "golden retriever", "copula_type") in shapes


def test_w2_capitalized_breed_ladder_derives_clean():
    """The capitalized-orthography atom now derives the FULL corgi-shaped ladder: the
    breed query's rung (golden retriever, also_known_as, biscuit) beside the pet edge."""
    triples = _derive("Our dog Biscuit is a Golden Retriever.")
    assert ("user", "dog", "golden retriever") in triples
    assert ("biscuit", "instance_of", "dog") in triples
    assert ("user", "has_pet", "biscuit") in triples
    assert ("golden retriever", "also_known_as", "biscuit") in triples


def test_w2_live_atom_and_full_turn_derive_the_ladder():
    """The EXACT live turn (em-dash, adopt clause) and its clean atom both derive the full
    ladder — the deterministic layers were never the live lowercase drop; pinned so a
    union-side loss can never mask a derive loss."""
    for text in (BREED_TURN, "Our dog Biscuit is a golden retriever.",
                 "Our dog Biscuit is a golden retriever"):
        triples = _derive(text)
        assert ("golden retriever", "also_known_as", "biscuit") in triples, text
        assert ("user", "has_pet", "biscuit") in triples, text
        assert ("user", "dog", "golden retriever") in triples, text
    assert ("user", "adopt", "biscuit") in _derive(BREED_TURN)


def test_w2_capitalized_matches_lowercase_parity():
    """Whatever the orthography, the same rung set lands (the wound was the divergence)."""
    cap = _derive("Our dog Biscuit is a Golden Retriever.")
    low = _derive("Our dog Biscuit is a golden retriever.")
    assert cap == low


def test_w2_flag_off_is_byte_parity_with_the_noun_only_scan():
    """COPULA_PROPN_KIND_BIND=OFF → the capitalized attr is invisible to the detector
    exactly as today (only the appos binding survives — the red-state receipt: the breed
    lives only in the user attr)."""
    old = L.COPULA_PROPN_KIND_BIND
    L.COPULA_PROPN_KIND_BIND = False
    try:
        binds = L.analyze_name_type_bindings(L._parse("Our dog Biscuit is a Golden Retriever."))
        shapes = {(b.name.lower(), b.type_noun.lower(), b.connector) for b in binds}
        assert ("biscuit", "dog", "appos") in shapes
        assert ("biscuit", "golden retriever", "copula_type") not in shapes
        triples = _derive("Our dog Biscuit is a Golden Retriever.")
        assert ("golden retriever", "also_known_as", "biscuit") not in triples
        assert ("user", "dog", "golden retriever") in triples  # the attr-scalar stands
    finally:
        L.COPULA_PROPN_KIND_BIND = old


def test_w2_free_floating_propn_attr_never_binds():
    """The negative guard: a PROPN copula attr with NO same-doc named-instance evidence
    (the subject is not an appos-bound name) never mints a kind binding."""
    for text in ("Biscuit is Rex.",
                 "Alderaan is Chandrila.",
                 "My wife Maren is Swiss."):
        binds = L.analyze_name_type_bindings(L._parse(text))
        assert not any(b.connector == "copula_type" and b.type_noun.lower() in
                       ("rex", "chandrila", "swiss") for b in binds), text


def test_w2_suppressor_still_spares_the_breed_rung():
    """The union shape: the attr-scalar claim and the landed ladder coexist — the
    (breed, also_known_as, name) rung survives suppress_name_type_binder_vs_attr_scalar
    for the two-word breed exactly as for corgi (BINDER_ALIAS_SPARE_NAMED_INSTANCE)."""
    edges = [
        {"subject": "user", "rel_type": "dog", "object": "golden retriever",
         "object_datatype": "string"},
        {"subject": "biscuit", "rel_type": "instance_of", "object": "dog"},
        {"subject": "user", "rel_type": "has_pet", "object": "biscuit"},
        {"subject": "golden retriever", "rel_type": "also_known_as", "object": "biscuit"},
        {"subject": "user", "rel_type": "adopt", "object": "biscuit"},
    ]
    kept = L.suppress_name_type_binder_vs_attr_scalar(edges)
    rels = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
    assert ("golden retriever", "also_known_as", "biscuit") in rels
    assert ("user", "dog", "golden retriever") in rels
    assert ("user", "has_pet", "biscuit") in rels


def test_w2_flags_are_declared_default_on():
    """Both new spine flags and the ingest flag exist, default ON, in the sources this
    file ships against."""
    assert 'SPINE_PET_REL_SUPERSENSE", "true"' in LING_SOURCE
    assert 'COPULA_PROPN_KIND_BIND", "true"' in LING_SOURCE


# ── WOUND 2, WAVE 3: the rung must never mint a rival entity at the ingest seam ─────

def test_w3_rung_batch_evidence_contract():
    """The EXACT live sentence's derived batch carries the bind evidence IN BATCH: the
    rung's object is the subject of a live instance_of edge (the same-doc appositive
    binding — the evidence _aka_rung_bind_edges reads), and every fact subject is an
    already-known surface (user / the pet) except the rung's own kind phrase — the ONE
    surface the ingest could fresh-mint as a rival, which the bind flips."""
    facts = L.derive_sentence_facts(L._parse(BREED_TURN), _REF) or []
    rels = {(f.subject, f.rel_type, str(f.object)) for f in facts}
    assert ("golden retriever", "also_known_as", "biscuit") in rels
    assert ("biscuit", "instance_of", "dog") in rels
    assert {s for s, _r, _o in rels} <= {"user", "biscuit", "golden retriever"}


def test_w3_helper_flips_the_rung_and_spares_known_surfaces():
    """Hermetic (stub cursor): _aka_rung_bind_edges flips EXACTLY the rung shape —
    (golden retriever, also_known_as, biscuit) → (biscuit, also_known_as,
    'golden retriever') — leaving every other edge untouched; a SUBJECT surface that is
    already a registered alias is never flipped (today's resolution stands); a
    non-also_known_as edge with the same surfaces is never flipped."""
    import src.api.main as M

    class _Cur:
        def __init__(self, registered):
            self._registered = registered
        def execute(self, *a, **k):
            pass
        def fetchone(self):
            return ("x",) if self._registered else None
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Db:
        def __init__(self, registered):
            self._registered = registered
        def cursor(self):
            return _Cur(self._registered)

    class _E:
        def __init__(self, s, r, o):
            self.subject, self.rel_type, self.object = s, r, o

    edges = [
        _E("user", "dog", "golden retriever"),
        _E("biscuit", "instance_of", "dog"),
        _E("user", "has_pet", "biscuit"),
        _E("golden retriever", "also_known_as", "biscuit"),   # the rung
    ]
    flips = M._aka_rung_bind_edges(_Db(registered=False), edges, {"biscuit"})
    assert flips == 1
    assert (edges[3].subject, edges[3].object) == ("biscuit", "golden retriever")
    for e in edges[:3]:
        assert e.subject in ("user", "biscuit")  # untouched

    # a registered subject surface keeps today's resolution (no flip)
    edges2 = [_E("golden retriever", "also_known_as", "biscuit")]
    assert M._aka_rung_bind_edges(_Db(registered=True), edges2, {"biscuit"}) == 0
    assert (edges2[0].subject, edges2[0].object) == ("golden retriever", "biscuit")

    # a non-rung shape never flips: object not a named instance of the turn
    edges3 = [_E("golden retriever", "also_known_as", "sam")]
    assert M._aka_rung_bind_edges(_Db(registered=False), edges3, {"biscuit"}) == 0
    # ...subject itself a named instance of the turn; ...not an also_known_as rel
    edges4 = [_E("golden retriever", "instance_of", "biscuit")]
    assert M._aka_rung_bind_edges(_Db(registered=False), edges4, {"biscuit", "golden retriever"}) == 0
    assert (edges4[0].subject, edges4[0].object) == ("golden retriever", "biscuit")


def test_w3_helper_is_wired_and_flag_gated_first():
    """Structural: the companion flag exists default ON, the helper is called at the
    ingest edge loop AFTER the named-instance surfaces are computed and BEFORE the
    per-edge loop, and the call is gated on the flag FIRST (OFF = byte-parity — the
    probe never runs)."""
    assert 'os.environ.get("AKA_RUNG_BIND_NAMED_INSTANCE", "true")' in MAIN_SOURCE
    assert "def _aka_rung_bind_edges(" in MAIN_SOURCE
    call_pos = MAIN_SOURCE.index("_aka_rung_bind_edges(db, edges, _named_instance_surfaces)")
    gate_pos = MAIN_SOURCE.index("if _AKA_RUNG_BIND_NAMED_INSTANCE and _named_instance_surfaces:")
    nis_pos = MAIN_SOURCE.index("_named_instance_surfaces = _turn_named_instance_surfaces(db, edges)")
    assert nis_pos < call_pos
    assert gate_pos < call_pos, "the flag gate must sit BEFORE the helper call (OFF = never runs)"
    # the helper itself never commits (the shared ingest transaction owns the write)
    import inspect
    import src.api.main as M
    assert "db.commit" not in inspect.getsource(M._aka_rung_bind_edges)
    assert "db.rollback" not in inspect.getsource(M._aka_rung_bind_edges)


_DSN = os.environ.get("POSTGRES_DSN", "")
_w3_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the rung DB gate stays dark")


@_w3_db
def test_w3_db_no_rival_mint_for_the_breed_surface():
    """DB-gated end-to-end on TEMP tables (rolled back): the LIVE wound's exact order —
    subject resolution BEFORE the identity lane — mints a RIVAL entity whose preferred
    alias is the breed with the pet's NAME as a secondary alias; with the bind, the
    rung resolves to the EXISTING pet entity and the breed registers as its
    NON-preferred alias. No new-entity mint for the breed surface."""
    import uuid
    import psycopg2
    from src.entity_registry.registry import EntityRegistry
    import src.api.main as M

    def _seed(cur):
        cur.execute("SET search_path TO pg_temp, public")
        cur.execute("CREATE TEMP TABLE entities (id text PRIMARY KEY, entity_type text,"
                    " created_at timestamptz DEFAULT now())")
        cur.execute("CREATE TEMP TABLE entity_aliases (entity_id text, alias text,"
                    " is_preferred boolean DEFAULT true, display_form text,"
                    " preference_source text DEFAULT 'unspecified',"
                    " PRIMARY KEY (entity_id, alias))")
        cur.execute("CREATE TEMP TABLE rel_types (rel_type text,"
                    " tail_types text[] DEFAULT '{}')")
        cur.execute("INSERT INTO rel_types VALUES ('also_known_as', '{SCALAR}')")
        _bid = str(uuid.uuid4())
        cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Animal')", (_bid,))
        cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred)"
                    " VALUES (%s, 'biscuit', true)", (_bid,))
        return _bid

    # (1) THE RED STATE — today's order without the bind mints the rival
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            biscuit_id = _seed(cur)
        conn.commit()
        reg = EntityRegistry(conn, schema_name="pg_temp")
        rival = reg.resolve("u1", "golden retriever", name_intent=False)
        reg.register_alias(rival, "biscuit", False, "Person", "unspecified")
        with conn.cursor() as cur:
            cur.execute("SELECT e.entity_type, a.alias, a.is_preferred"
                        " FROM entities e JOIN entity_aliases a ON a.entity_id = e.id"
                        " WHERE e.id = %s", (rival,))
            rows = cur.fetchall()
        assert rival != biscuit_id
        assert ("unknown", "golden retriever", True) in rows       # the rival's preferred
        assert ("unknown", "biscuit", False) in rows               # the pet's name stolen
    finally:
        conn.rollback()
        conn.close()

    # (2) THE FIXED STATE — the bind flips the rung; the breed never mints
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            biscuit_id = _seed(cur)
        conn.commit()

        class _E:
            def __init__(self, s, r, o):
                self.subject, self.rel_type, self.object = s, r, o

        facts = L.derive_sentence_facts(L._parse(BREED_TURN), _REF) or []
        edges = [_E(f.subject, f.rel_type, str(f.object)) for f in facts]
        rung = next(e for e in edges if e.rel_type == "also_known_as")
        assert M._aka_rung_bind_edges(conn, edges, {"biscuit"}) == 1
        assert (rung.subject, rung.object) == ("biscuit", "golden retriever")
        reg = EntityRegistry(conn, schema_name="pg_temp")
        subj = reg.resolve("u1", rung.subject, name_intent=False)
        assert subj == biscuit_id            # the EXISTING pet, never a fresh mint
        res = reg.register_alias(subj, rung.object, False, "Animal", "unspecified")
        assert res.get("written") and not res.get("preferred")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM entities")
            assert cur.fetchone()[0] == 1    # ONE entity — no rival mint for the breed
            cur.execute("SELECT alias, is_preferred FROM entity_aliases"
                        " ORDER BY alias")
            assert cur.fetchall() == [("biscuit", True), ("golden retriever", False)]
    finally:
        conn.rollback()
        conn.close()


# ── WOUND 2, WAVE 4: the breed ask must SERVE (the pet anchor's owner-class attr) ────
#
# THE LIVE CAPTURE (2026-11-13, fresh pre-prod seats, image 61ed60cd / main.py md5
# f60efbf7): the exact BREED_TURN now lands the FULL corgi-parity store through the live
# door — the /harvest-spans envelope carries all five ladder edges (incl. (golden
# retriever, also_known_as, biscuit)), /ingest commits adopt/has_pet/instance_of with
# dropped=[], the breed files as a NON-preferred alias on the biscuit entity, and the
# USER attr (dog, 'golden retriever') is live — yet "What breed is Biscuit?" serves only
# the relational neighborhood. The CORGI shape misses IDENTICALLY on an independent
# seat: the wound class is SERVE (the render never voices the owner's class-named attr
# on the pet, and a non-preferred alias never answers). FIX: RECALL_BREED_RIDES_OWNER_ATTR
# (default ON, OFF = byte-parity) — the breed ride voices the owner's (dog, <breed>) attr
# ON THE PET, gated at every leg on live metadata + the WordNet Animal supersense license
# (class AND value).

_UUID = "12345678-1234-5678-1234-567812345678"
_UUID2 = "87654321-4321-8765-4321-210987654321"


class _RideCur:
    """Stub cursor answering the breed ride's three reads in order: the P31 rel lookup,
    the kind's preferred alias, the owner's class-named attr rows."""

    def __init__(self, kind_alias, attr_rows):
        self._kind_alias, self._attr_rows = kind_alias, attr_rows
        self._step = 0

    def execute(self, sql, params=None):
        self._last_sql = sql
        self._step = 0 if "wikidata_pid = 'P31'" in sql else (
            1 if "entity_aliases" in sql else 2)

    def fetchone(self):
        if self._step == 1:
            return (self._kind_alias,) if self._kind_alias else None
        return None

    def fetchall(self):
        if self._step == 2:
            return list(self._attr_rows)
        if self._step == 0:
            return [("instance_of",)]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RideDb:
    def __init__(self, kind_alias, attr_rows):
        self._cur = _RideCur(kind_alias, attr_rows)

    def cursor(self):
        return self._cur

    def rollback(self):
        pass


def _ride_facts():
    return [
        {"subject": "user", "rel_type": "adopt", "object": _UUID},
        {"subject": _UUID, "rel_type": "instance_of", "object": _UUID2},
        {"subject": "user", "rel_type": "has_pet", "object": _UUID},
    ]


def test_w4_breed_ride_voices_owner_class_attr_on_the_pet():
    """THE DISCRIMINATING SHAPE: the walk's served neighborhood (instance_of dog +
    has_pet) + the owner's live (dog, 'golden retriever') attr → ONE ride row voicing
    the breed ON THE PET (subject = the pet entity, object = the breed surface)."""
    import src.api.main as M

    rows = M._breed_rides_owner_attr_rows(
        _RideDb("dog", [("dog", "golden retriever", None, None, None, "user_stated")]),
        _ride_facts(), "user-id-ignored-by-stub")
    assert len(rows) == 1
    r = rows[0]
    assert (r["subject"], r["rel_type"], r["object"]) == (_UUID, "dog", "golden retriever")
    assert r["source"] == "attributes"
    assert r["fact_class"] in ("A", "B", "C")
    # the corgi shape rides identically (the live A/B measured parity)
    rows_c = M._breed_rides_owner_attr_rows(
        _RideDb("dog", [("dog", "corgi", None, None, None, "user_stated")]),
        _ride_facts(), "user")
    assert [r["object"] for r in rows_c] == ["corgi"]


def test_w4_breed_ride_declines_without_the_metadata_licenses():
    """Every leg is metadata-coupled: no instance_of row → no ride; a NON-Animal class
    (car) → no ride even with a matching owner attr; a NON-Animal attr VALUE ('fluffy',
    '3') → no ride; no live owner attr → no ride."""
    import src.api.main as M

    # no pet-classed entity in the served neighborhood
    assert M._breed_rides_owner_attr_rows(
        _RideDb("dog", [("dog", "golden retriever", None, None, None, "user_stated")]),
        [{"subject": "user", "rel_type": "adopt", "object": _UUID}], "user") == []
    # non-Animal class: the WordNet license declines the kind surface
    assert M._breed_rides_owner_attr_rows(
        _RideDb("car", [("car", "corolla", None, None, None, "user_stated")]),
        _ride_facts(), "user") == []
    # Animal class but NON-Animal value (a scalar, not a breed-of-class)
    assert M._breed_rides_owner_attr_rows(
        _RideDb("dog", [("dog", "fluffy", None, None, None, "user_stated")]),
        _ride_facts(), "user") == []
    # no live attr row at all → the abstention stands
    assert M._breed_rides_owner_attr_rows(
        _RideDb("dog", []), _ride_facts(), "user") == []


def test_w4_flag_off_is_byte_parity_and_hook_is_flag_gated_first():
    """Structural (the house pattern): the flag is declared default ON; the phase-5 hook
    gates on the flag FIRST (OFF → the helper never runs → db_facts byte-identical); the
    helper never commits and rolls back on a swallowed read error."""
    import inspect
    import src.api.main as M

    assert '"RECALL_BREED_RIDES_OWNER_ATTR", "true"' in MAIN_SOURCE
    assert "def _breed_rides_owner_attr_rows(" in MAIN_SOURCE
    call_pos = MAIN_SOURCE.index("_breed_rides_owner_attr_rows(db, db_facts, user_id)")
    gate_pos = MAIN_SOURCE.index("if _RECALL_BREED_RIDES_OWNER_ATTR and db and db_facts:")
    assert gate_pos < call_pos, "the flag gate must sit BEFORE the helper call (OFF = never runs)"
    assert "db.commit" not in inspect.getsource(M._breed_rides_owner_attr_rows)
    assert "db_conn.rollback()" in inspect.getsource(M._breed_rides_owner_attr_rows)


def test_w4_breed_ride_is_fail_safe_on_db_error():
    """A swallowed SQL error → [] and a rollback (the bench-press lesson: a poisoned
    transaction must never blank the serve)."""
    import src.api.main as M

    class _Boom:
        def cursor(self):
            raise RuntimeError("db down")

        def rollback(self):
            self.rolled = True

    db = _Boom()
    assert M._breed_rides_owner_attr_rows(db, _ride_facts(), "user") == []
    assert db.rolled is True
