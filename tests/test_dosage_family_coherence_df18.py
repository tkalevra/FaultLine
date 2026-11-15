"""DOSAGE-FAMILY COHERENCE (issue #18) — ONE family resolution across the three seams.

THE BUG (measured live on deploy #9, reproduced in-process in
DEV/review/gauntlet/dosage-family-coherence/round_1/repro.txt): the dosage attribute
family was named differently at every seam —
  (a) INGEST take-frame stores `quantity` on the substance (#14 — correct);
  (b) INGEST copula frame WELDS the family noun into the attribute name on the SPEAKER
      ("My lisinopril dose is 10 milligrams" → (user, lisinopril_dose, …); the live
      LLM-atomized variant minted a 'Lisinopril Dosage' phrase ENTITY);
  (c) CORRECTION re-extraction names a fresh mint ('has_medication') — the #14 floor is
      gated to a NO-rel gap, every addressing rung declines on the mint, and the
      correction dies "Old relational fact not found: has_medication" (byte-for-byte
      reproduced on a second tenant with the 1st-person flatten subject='user');
  (d) QUERY — "What is my X dose/level?" binds only measure_verb ∪ the `quantity`
      constant, so a `*_level`/`*_dose` row is reachable only via fetch-all.

THE FIX (all cue-rail, NO word zoo — migration 280 `dosage_noun`, floor∪tenant):
  1. linguistics _attr_scalar_binding — SPINE_DOSAGE_FAMILY: a first-person possessed
     aspect whose HEAD is a dosage-family member rebinds possessor → the compound-child
     SUBSTANCE and attribute → the family CANONICAL (the cue row's keyed description,
     seed `quantity`) — the SAME name and entity the take-frame writes. A GENITIVE
     possessor ("my car's fuel level") anchors right already and is left byte-for-byte.
  2. main.py harvest preference-deferral parity — the mangled head-only preference twin
     is dropped also when its VERBATIM VALUE is already captured as the construction's
     scalar (the rebind moved the scalar off the slug, so (subject, rel) alone misses).
  3. main.py _address_scalar_target rung 4 — DOSAGE-FAMILY MAPPING: the guess (or the
     user's own correction words) names the family per the cue class → subject-scoped
     family rows (old-value numeric-key narrowed when >1), else the TENANT-WIDE family
     attribute whose stored value numeric-matches the old value UNIQUELY. Admission-only
     after every name/value rung; uniqueness-before-gate preserved (#12).
  4. main.py determine_path — the #14 mirror-frame fold admits the asked family noun
     through a compound attribute that CARRIES it (token equality ∪ the -age
     nominalization stem: dosage→dos binds lisinopril_dose).

FROZEN BARS (issue #18):
  1. Fresh seat M1→M4: "What is my lisinopril dosage?" renders 10 milligrams — no junk
     anchor, no phrase/value alias.
  2. M6 correction (the deploy-#9 mint shape) succeeds: 20 live, history kept; M7
     renders 20.
  3. The 1st-person flatten shape (subject='user') corrects the FAMILY row via the
     tenant-wide family_value mapping — previously the live refusal.
  4. M3 copula no longer welds the family noun into ANY name: no `*_dose`/`*_level`
     attribute on the speaker, no relational junk twin, no family-noun alias.
  5. Subject-agnostic variants: genitive "my car's fuel level" unchanged (car,
     fuel_level); first-person "my thyroid level is 2.1" resolves to (thyroid,
     quantity); "what is my thyroid level?" binds the family.
  6. No-dosage sentences unchanged: address attr-scalar, preference seam, aspirin.

Runs the REAL harvest → /ingest → /query → /retract/correct chain against the throwaway
POSTGRES_DSN tenant (correction brain mocked to the measured extraction shapes, exactly
like tests/test_dosage_frame_dz14.py). Skips without a test-named DSN — disclosed,
never a silent pass.
"""

import os
import time
import uuid

import pytest

# Applies a FAN-OUT migration (280, into every faultline_% schema) to the shared suite database:
# run with the other migration-appliers on one xdist worker (#44 suite hang).
pytestmark = pytest.mark.xdist_group("shared_migrations")

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

_M1 = "I take lisinopril 10 milligrams daily for high blood pressure."
_M3_LIS = "My lisinopril dose is 10 milligrams."
_M3_MET = "My metformin dose is 500 milligrams."
_M6 = "Actually, my lisinopril dose is 20 milligrams, not 10."
_V_FUEL = "My car's fuel level is a quarter."
_V_THYROID = "My thyroid level is 2.1."
_NO_DOSAGE = "I take aspirin for headaches."
_NO_DOSAGE_ADDR = "My address is 123 Main Street."

# the deploy-#9 live correction-extraction shape: the LLM DID name a relationship — a
# fresh mint that exists in no table. The #14 floor is gated to a NO-rel gap, so nothing
# in the pre-#18 code mapped it onto the stored family.
_EXTRACTION_MINT = {
    "subject_name": "lisinopril",
    "old_rel_type": "has_medication",
    "old_value": "10 milligrams",
    "new_rel_type": "has_medication",
    "new_value": "20 milligrams",
    "dimension": "RELATIONAL",
    "confidence": 0.9,
}
# the 1st-person FLATTEN shape (the correction-extraction LLM's measured behaviour —
# the NAME-REPAIR doc): subject collapses to the speaker, who holds no dosage row. This
# is the exact shape that died "Old relational fact not found: has_medication" live.
_EXTRACTION_MINT_FLAT = dict(_EXTRACTION_MINT, subject_name="user")

_ZOO = ("lisinopril dosage", "lisinopril dose", "10 milligrams", "20 milligrams",
        "500 milligrams", "metformin dose", "thyroid level")


# --------------------------------------------------------------------------- #
# 0. Unit seams — no DB required
# --------------------------------------------------------------------------- #


def test_dosage_family_cue_floor_is_the_discharged_owner_data_call():
    """The cue rail's seed floor: the five family members, each canonicalized to the
    attribute the #14 take-frame already writes. This ROW is the owner DATA call #14
    disclosed, discharged by this lane (migration 280 + the overlay floors)."""
    from src.api.linguistic_cue_overlay import (
        _BOOTSTRAP_DOSAGE_CANONICAL_MAP, _BOOTSTRAP_DOSAGE_NOUNS, DOSAGE_NOUN_CATEGORY,
        _BOOTSTRAP_BY_CATEGORY)
    assert _BOOTSTRAP_DOSAGE_NOUNS == frozenset(
        {"dose", "dosage", "quantity", "level", "amount"})
    assert set(_BOOTSTRAP_DOSAGE_CANONICAL_MAP) == set(_BOOTSTRAP_DOSAGE_NOUNS)
    assert set(_BOOTSTRAP_DOSAGE_CANONICAL_MAP.values()) == {"quantity"}
    # a registered floor is load-bearing: an unregistered category resolves EMPTY (and
    # used to borrow the naming-verb set — the _bootstrap_for default trap)
    assert _BOOTSTRAP_BY_CATEGORY.get(DOSAGE_NOUN_CATEGORY) == _BOOTSTRAP_DOSAGE_NOUNS


def test_migration_280_seeds_the_family_into_the_public_template():
    """The migration file exists, seeds exactly the floor family into public, and fans
    out to existing tenant schemas (the 279 pattern)."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "migrations", "280_linguistic_cues_dosage_noun.sql")
    assert os.path.exists(path), path
    sql = open(path).read()
    for member in ("dose", "dosage", "quantity", "level", "amount"):
        assert re.search(rf"\('{member}',\s*'dosage_noun',\s*'quantity'", sql), member
    assert "ON CONFLICT (cue, category) DO NOTHING" in sql
    assert "LIKE 'faultline\\_%'" in sql  # the existing-tenant fan-out loop


def test_copula_family_rebinds_to_substance_and_canonical_no_weld():
    """FROZEN BAR 4 (deriver level): the first-person copula frame files the dosage as
    the family canonical ON THE SUBSTANCE — the family noun is welded into NO name, and
    no mangled twin survives."""
    pytest.importorskip("spacy")
    from src.api.main import _build_typed_doc, _CANONICAL_ENTITY_TYPES, get_gliner_model
    from src.extraction import linguistics as L
    gmodel = get_gliner_model()

    def edges(text):
        doc = _build_typed_doc(text, gmodel, list(_CANONICAL_ENTITY_TYPES))
        out = set()
        for e in L.derive_sentence_facts(doc, None, None):
            d = e if isinstance(e, dict) else getattr(e, "__dict__", e)
            out.add((d["subject"], d["rel_type"], d["object"]))
        return out

    got = edges(_M3_LIS)
    assert ("lisinopril", "quantity", "10 milligrams") in got, got
    for s, r, _o in got:
        assert (s, r) != ("user", "lisinopril_dose"), got
        assert "dose" not in r and "dosage" not in r, got
    assert not any(s == "user" and r == "lisinopril_dose" for s, r, _o in got), got

    got_met = edges(_M3_MET)
    assert ("metformin", "quantity", "500 milligrams") in got_met, got_met
    assert not any("dose" in r or "dosage" in r for _s, r, _o in got_met), got_met


def test_copula_family_genitive_possessor_unchanged_and_open_slot():
    """FROZEN BAR 5: the GENITIVE frame anchors right already — "my car's fuel level"
    keeps (car, fuel_level) byte-for-byte (the compound child there is the aspect's
    QUALIFIER, not a substance) — while the first-person "my thyroid level is 2.1"
    resolves through the OPEN family slot (level → quantity on thyroid)."""
    pytest.importorskip("spacy")
    from src.api.main import _build_typed_doc, _CANONICAL_ENTITY_TYPES, get_gliner_model
    from src.extraction import linguistics as L
    gmodel = get_gliner_model()

    def edges(text):
        doc = _build_typed_doc(text, gmodel, list(_CANONICAL_ENTITY_TYPES))
        out = set()
        for e in L.derive_sentence_facts(doc, None, None):
            d = e if isinstance(e, dict) else getattr(e, "__dict__", e)
            out.add((d["subject"], d["rel_type"], d["object"]))
        return out

    got_fuel = edges(_V_FUEL)
    assert ("car", "fuel_level", "quarter") in got_fuel, got_fuel
    # the possessive OWNS edge (user, owns, car) is legitimate — the weld would be a
    # family-noun attribute on the speaker, and there is none
    assert not any(s == "user" and ("level" in r or "dose" in r or "dosage" in r)
                   for s, r, _o in got_fuel), got_fuel

    got_thy = edges(_V_THYROID)
    assert ("thyroid", "quantity", "2.1") in got_thy, got_thy
    assert not any("level" in r for _s, r, _o in got_thy), got_thy


def test_rebind_flag_off_is_byte_for_byte_legacy():
    """SPINE_DOSAGE_FAMILY=OFF → the flat slug emit runs exactly as before (the weld).
    The flag is a module-level constant (read once at import), so it is patched on the
    module — the same contract as the other spine flags in their own tests."""
    pytest.importorskip("spacy")
    from src.api.main import _build_typed_doc, _CANONICAL_ENTITY_TYPES, get_gliner_model
    from src.extraction import linguistics as L
    gmodel = get_gliner_model()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(L, "SPINE_DOSAGE_FAMILY", False, raising=False)
    try:
        doc = _build_typed_doc(_M3_LIS, gmodel, list(_CANONICAL_ENTITY_TYPES))
        out = set()
        for e in L.derive_sentence_facts(doc, None, None):
            d = e if isinstance(e, dict) else getattr(e, "__dict__", e)
            out.add((d["subject"], d["rel_type"], d["object"]))
        assert ("user", "lisinopril_dose", "10 milligrams") in out, out
    finally:
        monkey.undo()


def test_address_scalar_target_family_rungs_unit_seams():
    """Rung 4 in isolation (fake cursor). Rung priority is part of the contract: a
    family-member guess whose OLD VALUE still matches is caught first by rung 2's
    old-value anchor (the #14 admission — via='pin_subject'); the FAMILY rung owns the
    cases the value/name rungs decline — a value-drifted guess, and the deploy-#9 mint +
    flatten (the correction TEXT names the family while the subject holds no family row
    and exactly one tenant row carries the old value's numeric key). Ambiguity declines;
    non-family guesses never map; digit-less values never map tenant-wide."""
    import src.api.main as M

    class _FakeCur:
        def __init__(self, attrs):
            self._attrs = attrs  # list of (entity_id, attribute, value_text)
            self._last = None

        def execute(self, sql, params=()):
            self._last = (sql, params)

        def fetchone(self):
            sql, params = self._last
            if "SELECT 1 FROM entity_attributes" in sql:
                ent, attribute = params[0], params[1]
                hit = any(e == ent and a == attribute for (e, a, _v) in self._attrs)
                return (1,) if hit else None
            return None

        def fetchall(self):
            sql, params = self._last
            if "FROM entity_attributes" not in sql:
                return []
            if "WHERE attribute = %s" in sql:
                return [(e, v) for (e, a, v) in self._attrs
                        if a == params[0] and e != params[1]]
            if "WHERE entity_id = %s" in sql:
                return [(a, v) for (e, a, v) in self._attrs if e == params[0]]
            if "WHERE entity_id <> %s" in sql:
                return [(e, a, v) for (e, a, v) in self._attrs if e != params[0]]
            return []

    lis, user = "11111111-1111-5111-8111-111111111111", "22222222-2222-5222-8222-222222222222"
    other = "33333333-3333-5333-8333-333333333333"
    attrs = [(lis, "quantity", "10 milligrams"), (user, "address", "123 Main Street")]

    # (a) family guess + the old value still matching: rung 2's old-value anchor wins
    # (established priority) — the row is addressed either way
    s, a, via = M._address_scalar_target(_FakeCur(attrs), lis, "dose", "10 milligrams", user)
    assert (s, a) == (lis, "quantity") and via in ("pin_subject", "family"), (s, a, via)
    # (b) family guess whose old value DRIFTED (stored 10, user said "not 15"): the
    # value/name rungs decline → the FAMILY rung maps the subject's unique family row
    s, a, via = M._address_scalar_target(_FakeCur(attrs), lis, "dose", "15 milligrams", user)
    assert (s, a, via) == (lis, "quantity", "family"), (s, a, via)
    # (c) the deploy-#9 mint + flatten: the correction TEXT names the family, the subject
    # holds no family row → the tenant-wide UNIQUE numeric-value match finds it
    s, a, via = M._address_scalar_target(
        _FakeCur(attrs), user, "has_medication", "10 milligrams", user,
        text="Actually, my lisinopril dose is 20 milligrams, not 10.")
    assert (s, a, via) == (lis, "quantity", "family_value"), (s, a, via)
    # (d) AMBIGUITY DECLINES: two family rows carry the value → no map (today's refusal)
    s, a, via = M._address_scalar_target(
        _FakeCur(attrs + [(other, "quantity", "10 milligrams")]),
        user, "has_medication", "10 milligrams", user,
        text="Actually, my lisinopril dose is 20 milligrams, not 10.")
    assert via is None, (s, a, via)
    # (e) A NON-FAMILY correction never maps: no family token in the guess nor in the
    # correction text — today's refusal stands
    s, a, via = M._address_scalar_target(
        _FakeCur(attrs), user, "has_pet", None, user, text="my dog is a morkie, not a poodle")
    assert via is None, (s, a, via)
    # (f) digit-less values never map tenant-wide (the numeric key is the row identity)
    s, a, via = M._address_scalar_target(
        _FakeCur([(lis, "quantity", "a quarter")]), user, "has_medication",
        "a quarter", user, text="my dose is half, not a quarter")
    assert via is None, (s, a, via)


def test_mirror_frame_family_token_bind_unit():
    """The #14 mirror frame still returns the aspect head for the family nouns — the
    frame grammar is unchanged; the FAMILY decision lives at the admission fold."""
    from src.api.main import _is_measure_aspect_np_subject
    assert _is_measure_aspect_np_subject("What is my thyroid level?") == "level"
    assert _is_measure_aspect_np_subject("What is my metformin dose?") == "dose"
    assert _is_measure_aspect_np_subject("What is my lisinopril dosage?") == "dosage"


# --------------------------------------------------------------------------- #
# 1. E2E — the fresh-seat chain, rows read back (EP1 shape, in-process)
# --------------------------------------------------------------------------- #

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")


@pytest.fixture()
def df18_tenant(monkeypatch):
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    # conftest.py's baseline turns SAAS_MODE ON for the whole session; the dz14 fixture
    # deletes it the same way — these bars pin the open-core ingest path.
    monkeypatch.delenv("SAAS_MODE", raising=False)
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(_DSN)
    try:
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
    finally:
        conn.close()
    yield user_id, schema
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


def _rows(schema, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture()
def df18_client(monkeypatch):
    import src.api.main as M
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True

    class _D:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def setex(self, k, t, v):
            self.d[k] = v
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()

    M._idempotency_mgr = _Idem()

    with TestClient(M.app) as client:
        from src.extraction import linguistics as _L
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < 120:
            time.sleep(0.5)
        assert _L._nlp is not None, "spaCy layer never loaded — the spine would no-op"
        yield client


def _ingest(client, user_id, text):
    h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
    assert h.status_code == 200, h.text[:300]
    edges = h.json().get("edges") or []
    r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                     "edges": edges, "source": "mcp"})
    assert r.status_code == 200, r.text[:300]
    return edges


def _query_rels(client, user_id, text):
    q = client.post("/query", json={"text": text, "user_id": user_id}).json()
    return q, {(f.get("subject"), f.get("rel_type"), f.get("object"))
               for f in (q.get("facts") or [])}


def test_e2e_ingest_stores_one_family_name_no_weld_anywhere(df18_tenant, df18_client):
    """FROZEN BAR 4 (store level): M1 + M3 copulas + variants leave ONE family row per
    dosage — the canonical `quantity` on the substance; NO welded compound attribute on
    the speaker; NO relational junk twin carrying a family-noun rel; NO family-noun
    alias. The genitive variant keeps (car, fuel_level) — anchored right, qualifier kept."""
    user_id, schema = df18_tenant
    for text in (_M1, _M3_LIS, _M3_MET, _V_FUEL, _V_THYROID):
        _ingest(df18_client, user_id, text)

    lisinopril_id = _rows(schema, "SELECT entity_id FROM entity_aliases "
                                  "WHERE alias = 'lisinopril'")[0][0]
    metformin_id = _rows(schema, "SELECT entity_id FROM entity_aliases "
                                 "WHERE alias = 'metformin'")[0][0]
    thyroid_id = _rows(schema, "SELECT entity_id FROM entity_aliases "
                               "WHERE alias = 'thyroid'")[0][0]
    car_id = _rows(schema, "SELECT entity_id FROM entity_aliases "
                           "WHERE alias = 'car'")[0][0]

    attrs = {(r[0], r[1], r[2]) for r in _rows(
        schema, "SELECT entity_id, attribute, value_text FROM entity_attributes "
                "WHERE superseded_at IS NULL")}
    assert (lisinopril_id, "quantity", "10 milligrams") in attrs, attrs
    assert (metformin_id, "quantity", "500 milligrams") in attrs, attrs
    assert (thyroid_id, "quantity", "2.1") in attrs, attrs
    assert (car_id, "fuel_level", "quarter") in attrs, attrs
    # the weld: NO attribute named after the family-noun compound on the SPEAKER
    for bad in ("lisinopril_dose", "metformin_dose", "thyroid_level"):
        assert not any(a == bad for (_e, a, _v) in attrs), attrs

    # no relational junk twin with a family-noun rel
    fam_rels = [r for (r,) in _rows(
        schema, "SELECT DISTINCT rel_type FROM facts "
                "WHERE rel_type ~ '(^|_)(dose|dosage|level|amount)(_|$)'")]
    assert not fam_rels, fam_rels

    aliases = {r[0] for r in _rows(schema, "SELECT alias FROM entity_aliases")}
    for bad in _ZOO:
        assert bad not in aliases, (bad, sorted(aliases))


def test_e2e_full_chain_m1_to_m7_renders_then_corrects_then_renders(
        df18_tenant, df18_client, monkeypatch):
    """FROZEN BARS 1 + 2 + 3, one seat: M1→M4 renders 10 milligrams (no junk anchor);
    the deploy-#9 MINT correction (subject=lisinopril) supersedes to 20 with history;
    M7 renders 20; the FLATTEN correction (subject=user — the live refusal shape) now
    corrects the FAMILY row via the tenant-wide family_value mapping."""
    import src.api.main as M
    user_id, schema = df18_tenant
    _ingest(df18_client, user_id, _M1)
    _ingest(df18_client, user_id, _M3_LIS)
    lisinopril_id = _rows(schema, "SELECT entity_id FROM entity_aliases "
                                  "WHERE alias = 'lisinopril'")[0][0]

    # M4 — the interrogative renders the scalar (both ingest lanes agreed on ONE row)
    _q, got = _query_rels(df18_client, user_id, "What is my lisinopril dosage?")
    assert ("lisinopril", "quantity", "10 milligrams") in got, got

    # M6 — the deploy-#9 mint shape, subject correctly lisinopril
    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(_EXTRACTION_MINT))
    r = df18_client.post("/retract/correct", json={"text": _M6, "user_id": user_id,
                                                   "attested": True})
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["status"] == "corrected", out
    assert out["old_rel_type"] == "quantity" and out["new_rel_type"] == "quantity", out
    hist = _rows(schema, "SELECT attribute, prior_value_text, new_value_text "
                         "FROM entity_attributes_history")
    assert ("quantity", "10 milligrams", "20 milligrams") in [tuple(h) for h in hist], hist

    # M7 — renders 20
    _q, got = _query_rels(df18_client, user_id, "What is my lisinopril dosage?")
    assert ("lisinopril", "quantity", "20 milligrams") in got, got
    assert not any(o == "10 milligrams" for (_s, _r, o) in got), got
    assert str(lisinopril_id) == str(lisinopril_id)  # keep the resolver honest, trivially

    # M6' — the FLATTEN shape (subject='user'): the live deploy-#9 refusal. The user
    # holds no dosage row; exactly one family attribute in the tenant carries the OLD
    # value ("10 milligrams")… which is now superseded — the row reads 20, so the numeric
    # match declines and the correction refuses LOUDLY (no junk mint, nothing wrong is
    # written). That is the honest repeat-correction answer; the VALUE-bearing flatten
    # case is pinned on a fresh seat in the next test.
    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(_EXTRACTION_MINT_FLAT))
    r = df18_client.post("/retract/correct", json={"text": _M6, "user_id": user_id,
                                                   "attested": True})
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["status"] == "failed", out
    assert "not found" in (out.get("message") or "").lower(), out
    attrs = _rows(schema, "SELECT entity_id, attribute, value_text FROM entity_attributes "
                          "WHERE superseded_at IS NULL AND attribute = 'quantity'")
    assert attrs == [(lisinopril_id, "quantity", "20 milligrams")], attrs


def test_e2e_flatten_correction_maps_the_family_row_by_value(df18_tenant, df18_client,
                                                             monkeypatch):
    """FROZEN BAR 3 (the live deploy-#9 wound): M1 only (nothing on the speaker), the
    correction arrives with subject='user' + the fresh mint 'has_medication' — the exact
    shape that failed "Old relational fact not found: has_medication" live. The family
    rung maps it onto (lisinopril, quantity) by the old value's numeric key and the
    correction lands."""
    import src.api.main as M
    user_id, schema = df18_tenant
    _ingest(df18_client, user_id, _M1)
    lisinopril_id = _rows(schema, "SELECT entity_id FROM entity_aliases "
                                  "WHERE alias = 'lisinopril'")[0][0]
    assert not _rows(schema, "SELECT 1 FROM entity_attributes WHERE entity_id = %s",
                     (user_id,)), "precondition: the speaker holds no attribute"

    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(_EXTRACTION_MINT_FLAT))
    r = df18_client.post("/retract/correct", json={"text": _M6, "user_id": user_id,
                                                   "attested": True})
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["status"] == "corrected", out
    assert out["subject_uuid"] == lisinopril_id, out
    assert out["old_rel_type"] == "quantity" and out["new_rel_type"] == "quantity", out
    attrs = _rows(schema, "SELECT entity_id, attribute, value_text, provenance "
                          "FROM entity_attributes WHERE superseded_at IS NULL")
    assert attrs == [(lisinopril_id, "quantity", "20 milligrams", "user_corrected")], attrs
    hist = _rows(schema, "SELECT attribute, prior_value_text, new_value_text "
                         "FROM entity_attributes_history")
    assert ("quantity", "10 milligrams", "20 milligrams") in [tuple(h) for h in hist], hist


def test_e2e_variant_interrogatives_bind_the_family(df18_tenant, df18_client):
    """FROZEN BAR 5 (query): the family interrogatives bind the asked family — "what is
    my thyroid level?" renders thyroid's quantity; "what is my metformin dose?" renders
    metformin's; the genitive fuel level stays reachable exactly as before."""
    user_id, _schema = df18_tenant
    for text in (_M3_MET, _V_FUEL, _V_THYROID):
        _ingest(df18_client, user_id, text)

    _q, got = _query_rels(df18_client, user_id, "What is my thyroid level?")
    assert ("thyroid", "quantity", "2.1") in got, got
    assert not any("metformin" == s for (s, _r, _o) in got), got

    _q, got = _query_rels(df18_client, user_id, "What is my metformin dose?")
    assert ("metformin", "quantity", "500 milligrams") in got, got
    assert not any("thyroid" == s for (s, _r, _o) in got), got

    _q, got = _query_rels(df18_client, user_id, "What is my car's fuel level?")
    assert ("car", "fuel_level", "quarter") in got, got


def test_e2e_no_dosage_sentences_unchanged(df18_tenant, df18_client):
    """FROZEN BAR 6: the family rail must not touch non-family constructions — the
    address attr-scalar keeps its own attribute name on the speaker, aspirin takes no
    scalar, and the preference head-only mangle drop still fires for a non-rebound
    literal ("my address is …" keeps (user, address))."""
    user_id, schema = df18_tenant
    _ingest(df18_client, user_id, _NO_DOSAGE)
    _ingest(df18_client, user_id, _NO_DOSAGE_ADDR)

    attrs = {(r[0], r[1], r[2]) for r in _rows(
        schema, "SELECT entity_id, attribute, value_text FROM entity_attributes "
                "WHERE superseded_at IS NULL")}
    assert not any(a == "quantity" for (_e, a, _v) in attrs), attrs
    user_attrs = {a for (e, a, _v) in attrs if e == user_id}
    assert "address" in user_attrs, attrs
    aliases = {r[0] for r in _rows(schema, "SELECT alias FROM entity_aliases")}
    assert "aspirin" in aliases
    for bad in ("aspirin for headaches", "dose", "dosage"):
        assert bad not in aliases, sorted(aliases)
