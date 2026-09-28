"""SEED ∪ TENANT for the keyed cue maps — the floor must WIDEN, never be DISCARDED.

THE DEFECT (measured, then reproduced locally row-for-row):
``linguistic_cue_overlay._resolve_keyed_map`` read

    tenant_map = _fetch_keyed_map(dsn, schema_name, category)
    if not tenant_map:
        tenant_map = dict(bootstrap)

— REPLACE, not union. The in-code floor applied ONLY when the tenant held ZERO rows in the
category. The moment a tenant grew ONE row the ENTIRE seeded floor was discarded. Local tenant
``faultline_<seat-uuid>`` holds social_role = {agent, colleague} and
therefore resolved WITHOUT the floor's ``friend -> friend_of``, so "my friend is Sam" fell through
to has_role + ``owns(user, sam)`` — a PERSON filed as an owned object — on a tenant whose only sin
was to grow. A growth mechanism that ERODES seeded knowledge as the tenant grows gets worse the
more the product is used.

This contradicted the documented contract (CLAUDE.md: the overlays resolve "seed u tenant rows
(tenant overrides)"). The sibling overlays get away with a TENANT-ONLY read because provisioning
COPIES ``public`` into the tenant schema, so their union is realised at provisioning time. That
does not hold here: several keyed floors (social_role, cessative_verb, ...) have NO rows in
``public.linguistic_cues`` at all, so the in-code floor is their ONLY carrier.

WHAT IS PINNED
  1. UNION — a floor key absent from the tenant still resolves when the tenant holds other rows.
  2. TENANT WINS on key collision (authority order: user > seed > growth).
  3. SUPPRESSION — a cue the tenant DEACTIVATED (is_active=false) is NOT resurrected by the floor.
     This is the regression the union would otherwise introduce: ``linguistic_cues`` has no DELETE
     path, the documented correction is ``SET is_active = false``, and an in-code floor has no
     is_active column.
  4. FAIL-SAFE — an unreadable tenant still resolves the floor, never another tenant, never public.
  5. UNREGISTERED CATEGORY — ``_bootstrap_for`` returns EMPTY and logs critical; it must NOT fall
     back to the naming-verb set (a WRONG floor is worse than an empty one).

These pins are DB-FREE: they drive the resolver with a monkeypatched fetch seam, and the DEFAULT
(un-monkeypatched) behaviour of ``_bootstrap_for`` is pinned separately in
``test_bootstrap_for_*`` so a monkeypatch can never stand in for the real constant.
"""
import re
import pytest

from src.api import linguistic_cue_overlay as lco
from src.api import rel_type_overlay


CAT = "unit_scalar"
FLOOR = {"year": "age", "foot": "height", "pound": "weight"}
DSN = "postgresql://unused/unused"
SCHEMA = "faultline_pin_tenant"


@pytest.fixture(autouse=True)
def _bind_and_clear():
    lco.invalidate()
    tok = rel_type_overlay.set_current_schema(SCHEMA)
    yield
    rel_type_overlay.reset_current_schema(tok)
    lco.invalidate()


def _patch(monkeypatch, active, suppressed=()):
    def _fake(dsn, schema, category):
        assert schema == SCHEMA, f"a bound tenant must never read {schema!r}"
        assert category == CAT
        return dict(active), frozenset(suppressed)
    monkeypatch.setattr(lco, "_fetch_keyed_map_with_suppressions", _fake)


def test_floor_survives_when_the_tenant_holds_rows(monkeypatch):
    """(1) THE DEFECT ITSELF. A tenant holding one grown row keeps the whole seeded floor."""
    _patch(monkeypatch, {"furlong": "distance"})
    got = lco._resolve_keyed_map(DSN, CAT, FLOOR)
    assert got == {"year": "age", "foot": "height", "pound": "weight", "furlong": "distance"}, got
    # ABLATION GUARD: under the old REPLACE semantics this would have been {"furlong": "distance"}.
    assert set(FLOOR).issubset(got), "the seeded floor was discarded — REPLACE semantics are back"


def test_tenant_wins_on_key_collision(monkeypatch):
    """(2) AUTHORITY ORDER user > seed > growth: the floor may widen, never overrule."""
    _patch(monkeypatch, {"year": "duration"})
    got = lco._resolve_keyed_map(DSN, CAT, FLOOR)
    assert got["year"] == "duration", got
    assert got["foot"] == "height", got


def test_a_deactivated_cue_is_not_resurrected_by_the_floor(monkeypatch):
    """(3) THE REGRESSION THE UNION WOULD OTHERWISE INTRODUCE.

    ``linguistic_cues`` has no user-facing DELETE; the documented correction is
    ``UPDATE linguistic_cues SET is_active = false`` (linguistics.py:6929), and every cue-seed
    migration is ``ON CONFLICT (cue, category) DO NOTHING`` so a re-run cannot blow the row over.
    An in-code floor has no is_active column, so a naive union would hand the user's switched-off
    cue straight back. The floor speaks only where the tenant is SILENT.
    """
    _patch(monkeypatch, {"furlong": "distance"}, suppressed={"foot"})
    got = lco._resolve_keyed_map(DSN, CAT, FLOOR)
    assert "foot" not in got, f"a deactivated cue came back from the code floor: {got}"
    assert got == {"year": "age", "pound": "weight", "furlong": "distance"}, got


def test_empty_tenant_still_resolves_the_whole_floor(monkeypatch):
    _patch(monkeypatch, {})
    assert lco._resolve_keyed_map(DSN, CAT, FLOOR) == FLOOR


def test_unreadable_tenant_fails_safe_to_the_floor(monkeypatch):
    """(4) FAIL-SAFE: never another tenant, never public — the in-code floor."""
    def _boom(dsn, schema, category):
        raise RuntimeError("relation does not exist")
    monkeypatch.setattr(lco, "_fetch_keyed_map_with_suppressions", _boom)
    assert lco._resolve_keyed_map(DSN, CAT, FLOOR) == FLOOR


def test_no_dsn_resolves_the_floor(monkeypatch):
    assert lco._resolve_keyed_map("", CAT, FLOOR) == FLOOR


def test_the_result_is_a_fresh_dict_not_the_floor_constant(monkeypatch):
    """The merge must not hand back (or mutate) the module-level bootstrap constant."""
    _patch(monkeypatch, {"furlong": "distance"})
    got = lco._resolve_keyed_map(DSN, CAT, FLOOR)
    got["scratch"] = "x"
    assert "scratch" not in FLOOR


# ── _bootstrap_for DEFAULTS — pinned WITHOUT monkeypatching, per the brief ──────────────────────

def test_bootstrap_for_unregistered_category_is_empty_not_the_naming_verbs():
    """(5) A WRONG FLOOR IS WORSE THAN AN EMPTY ONE.

    ``_bootstrap_for`` used to return ``_BOOTSTRAP_NAMING_VERBS`` for ANY unregistered category, so
    a category missing from ``_BOOTSTRAP_BY_CATEGORY`` silently resolved name/call/dub/christen as
    its own class on every cold or DB-down tenant. Two registry entries carried standing comments
    saying they existed ONLY to dodge that default — the signature of a rule that should never have
    defaulted.
    """
    got = lco._bootstrap_for("a_category_that_is_not_registered")
    assert got == frozenset(), got
    assert "name" not in got and "dub" not in got


def test_bootstrap_for_registered_categories_return_their_own_floor():
    assert lco._bootstrap_for(lco.NAMING_VERB_CATEGORY) is lco._BOOTSTRAP_NAMING_VERBS
    assert lco._bootstrap_for(lco.KINSHIP_NOUN_CATEGORY) is lco._BOOTSTRAP_KINSHIP_NOUNS
    # A DELIBERATELY EMPTY registered floor is unaffected by the unregistered-category change.
    assert lco._bootstrap_for(lco.ATTRIBUTE_NOUN_CATEGORY) == frozenset()


def test_every_set_category_constant_is_registered():
    """The unregistered path is now a fault, so nothing shipped may be relying on it.

    THIN_TYPE / UNIT_SCALAR / KINSHIP_GENDER / SOCIAL_ROLE / ROLE_NOUN / ALIAS_PREDICATE are KEYED
    classes: they never reach ``_bootstrap_for`` (``_resolve_keyed_map`` takes its floor as an
    argument), so they are legitimately absent from the SET registry.
    """
    keyed_only = {
        lco.THIN_TYPE_CATEGORY, lco.UNIT_SCALAR_CATEGORY, lco.KINSHIP_GENDER_CATEGORY,
        lco.SOCIAL_ROLE_CATEGORY, lco.ROLE_NOUN_CATEGORY, lco.ALIAS_PREDICATE_CATEGORY,
    }
    missing = []
    for name in dir(lco):
        if not name.endswith("_CATEGORY"):
            continue
        cat = getattr(lco, name)
        if not isinstance(cat, str) or cat in keyed_only:
            continue
        if cat not in lco._BOOTSTRAP_BY_CATEGORY:
            missing.append((name, cat))
    assert not missing, f"SET categories with no registered floor: {missing}"


# ── STEP 2: the social_role SEED (migration 272) — pinned on the CODE FLOOR, DB-free ────────────
#
# social_role measured n=0 on a fresh seat while role_noun=4 and kinship_noun=23, so the
# possessed-person-role rail was INERT on every virgin tenant and "My colleague works late." emitted
# (user, owns, colleague) — a PERSON filed as an OWNED OBJECT. The inventory is WordNet-derived, not
# invented: the internal design record These pin the in-code floor, which is what a
# DB-down / pre-migration tenant resolves; the DB seed itself is migration 272.

def _migration_272_social_roles() -> dict:
    """The DB RAIL is the carrier — parse migration 272 rather than a code literal.

    ⚠️ RE-POINTED 2026-08-27. This pair used to assert the closed class lived in
    ``lco._BOOTSTRAP_SOCIAL_ROLE_MAP`` — i.e. 45 role words enumerated IN CODE. That is the
    "domain word zoo" this project forbids outright (subject-agnostic & growable: lexicon lives
    in the per-tenant ``linguistic_cues`` growth rail, never in the engine), and it was already
    pinned against by tests/test_linguistics.py::test_carved_class_bootstraps_are_empty, which
    those assertions turned RED. Two test sets asserting opposite things is the tell.
    Measured: public.linguistic_cues carries 48 social_role rows on BOTH pre-prod and prod, so
    the rail is live and the in-code copy was redundant as well as forbidden.
    """
    import pathlib
    sql = pathlib.Path("migrations/272_linguistic_cues_social_role.sql").read_text()
    return dict(re.findall(r"\('([a-z]+)',\s*'social_role',\s*'([a-z_]+)'", sql))


def test_social_role_closed_class_is_carried_by_the_DB_RAIL_not_by_code():
    rail = _migration_272_social_roles()
    for cue in ("colleague", "coworker", "teammate", "roommate", "classmate", "friend"):
        assert cue in rail, f"{cue} missing from migration 272 — the rail goes inert for it"
    assert rail["friend"] == "friend_of", rail["friend"]
    assert rail["colleague"] == "knows", rail["colleague"]
    # ...and the IN-CODE floor stays the universal social primitive ONLY.
    assert lco._BOOTSTRAP_SOCIAL_ROLE_MAP == {"friend": "friend_of"}, \
        "a role lexicon was re-added to code; it belongs in a migration (see the docstring above)"


def test_social_role_floor_holds_no_occupations():
    """THE 'too wide' TEST. Occupations are OPEN-CLASS subject matter a tenant grows; they sit under
    worker.n.01 / professional.n.01, outside the derivation's anchor closure."""
    m = _migration_272_social_roles()
    for cue in ("dermatologist", "plumber", "engineer", "nurse", "barista", "accountant"):
        assert cue not in m, f"{cue} is an occupation — the derivation filter is too wide"


def test_social_role_floor_does_not_collide_with_a_sibling_cue_class():
    """A CROSS-CLASS COLLISION IS A DIRECTION BUG, AND THIS PIN IS WHY THE CLASS HAS ONE.

    `_possessed_person_role_rel` resolves kinship_noun BEFORE social_role, and
    `_person_role_relation` resolves social_role BEFORE role_noun. A lemma in two classes does not
    win by being more specific — it wins by ORDER, silently suppressing the other class's reading.
    Two real instances were caught this way and both are fixed:
      * `partner` — social_role would have shadowed kinship_noun's `partner -> spouse`. It got in
        because the derivation's first cut used a HAND-TYPED kinship list; the script now reads the
        sibling classes from public.linguistic_cues.
      * `boss` / `manager` — migration 117 also seeds social_role and included them, while role_noun
        already held them. `my manager` would have resolved `knows` instead of the role-derived
        `manager_of` that `_person_role_relation`'s docstring specifies. Migration 117 is amended and
        migration 272 Part 0 removes the rows already inserted.
    """
    social = set(lco._BOOTSTRAP_SOCIAL_ROLE_MAP)
    kin = set(lco._BOOTSTRAP_KINSHIP_NOUNS) | set(lco._BOOTSTRAP_KINSHIP_REL_MAP)
    role = set(lco._BOOTSTRAP_ROLE_NOUN_MAP)
    assert not (social & kin), f"social_role/kinship_noun collision: {sorted(social & kin)}"
    assert not (social & role), f"social_role/role_noun collision: {sorted(social & role)}"


def test_social_role_values_are_seeded_rel_types_only():
    """The map value is a rel_type the WGM gate must be able to dispose. Only two appear, both
    SEEDED and SYMMETRIC: `knows` (the generic person tie the growth engine itself writes —
    re_embedder `_CARVED = {"social_role": "knows"}`) and `friend_of` for the one universal."""
    _vals = set(_migration_272_social_roles().values())
    assert _vals == {"knows", "friend_of"}, sorted(_vals)
