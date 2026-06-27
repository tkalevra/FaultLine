"""Per-tenant linguistic_cues resolution (the GROWABLE linguistic-verb cue engine).

ARCHITECTURE (see migration 105, CLAUDE.md per-tenant overlay sections, and the sibling modules
`rel_type_overlay.py` / `taxonomy_overlay.py` / `temporal_pattern_overlay.py`):

FaultLine is per-tenant. `public.linguistic_cues` is a TEMPLATE / SEED-SOURCE ONLY, read solely by
provisioning (and the unscoped boot/anonymous fallback path here). The seeder copies public →
tenant at provisioning time, so each tenant's own schema carries the EVIDENCED naming-verb seed
PLUS any freq-gated grown cues. Growth NEVER writes to public, and NO runtime read touches public on
a bound tenant.

THE GAP THIS CLOSES:
`linguistics.analyze_naming` / `_event_title` / `is_naming_predicate` matched the modifying verb's
lemma against a FROZEN in-code two-word set (`_NAMING_VERB_LEMMAS = {"name","call"}`). A frozen list
assumes a fixed naming vocabulary and silently drops every other English naming verb ("titled",
"dubbed", "christened", …). This module resolves the naming-verb inventory from the BOUND TENANT
SCHEMA so a tenant's grown cues are honoured at runtime — exactly the metadata-driven, per-tenant,
growable contract the rel_type / taxonomy / temporal / extraction_patterns layers already obey.

WHAT THIS RESOLVES:
    <tenant>.linguistic_cues  (seed-copied-at-provisioning ∪ grown)  WHERE category='naming_verb'
The dependency RELATIONS (acl/relcl/compound/appos/oprd/attr/dobj) + the universal POS function-word
set stay in code (grammar, a language primitive); only the naming-VERB lemma recognition is data.

It deliberately MIRRORS `temporal_pattern_overlay.py` and REUSES the SAME request-schema ContextVar
(`rel_type_overlay._current_schema`, via `set_current_schema`) so a single per-request binding
governs ALL the overlays. The only module-level state here is the unscoped-fallback cache and the
per-tenant cache, exactly as in the sibling modules.

FAIL-SAFE: a tenant schema that predates this migration (no `linguistic_cues` table) — or any read
failure — resolves to the BOOTSTRAP naming-verb set (the in-code seed, hard-coded here as a DB-DOWN
safety net, NOT as the authority). It NEVER falls back to another tenant's rows, and it NEVER returns
an empty set (that would silently lose naming detection — the very brittleness this closes).

HOT-PATH COST: identical contract to the sibling overlays — unscoped fallback cached with a TTL,
per-tenant data cached per schema, a warm hit is DB-free.
"""

import time
import threading

import psycopg2
import structlog

# Reuse the SAME request-schema ContextVar binding as the rel_type/taxonomy/temporal overlays so ONE
# set_current_schema()/reset_current_schema() per request governs ALL overlays.
from src.api import rel_type_overlay

log = structlog.get_logger()

# TTL for both the global seed cache and per-schema overlays. Matches the sibling-overlay contract;
# explicit invalidation closes the loop faster than the TTL.
_TTL_SECONDS = 5.0

_lock = threading.RLock()

# Unscoped-fallback cache (public template) — used ONLY when no tenant schema is bound
# (boot / anonymous). NEVER consulted on a real tenant binding. Keyed BY CATEGORY so the naming,
# lvc_support, and svo_particle classes never collide in one slot.
# {category: {"cues": frozenset[str], "loaded_at": float}}
_seed_cache: dict[str, dict] = {}

# Per-schema cache: {schema_name: {"cues": frozenset[str], "loaded_at": float}}.
_overlay_cache: dict[str, dict] = {}

# ── BOOTSTRAP naming-verb set (DB-DOWN SAFETY NET ONLY — NOT the authority) ──────────
# This is the EVIDENCED seed inventory (the same rows migration 105 writes to public), hard-coded so
# a tenant schema lacking the table (pre-migration) or an unreadable read still classifies the
# naming/dubbing construction instead of silently dropping it. The DB rows are the authority; this is
# the fallback when the DB cannot be read. It SUPERSETS the retired in-code {"name","call"} so the
# fail-safe is never weaker than today's behavior.
_BOOTSTRAP_NAMING_VERBS: frozenset[str] = frozenset({
    "name", "call", "title", "dub", "entitle", "christen",
    "designate", "term", "label", "nickname",
})

# ── BOOTSTRAP light/support-verb (LVC) set — DB-DOWN SAFETY NET ONLY ─────────────────
# The small grammatical class of English "light"/support verbs that form a light-verb construction
# by governing an eventive complement ("have a meeting", "go to a concert", "attend a workshop",
# "take a trip", "do an interview", "make a visit", "participate in a webinar"). A lexical-aspect
# (grammatical) class, NOT a domain event list — membership is corroborated downstream by the parse
# (the eventive noun must be the verb's governed object/pobj). Hard-coded so a pre-migration / DB-down
# turn still recognizes the LVC instead of silently dropping the occurrence. Mirrors the retired
# in-code `linguistics._LVC_SUPPORT_VERB_LEMMAS`.
_BOOTSTRAP_LVC_SUPPORT_VERBS: frozenset[str] = frozenset({
    "have", "go", "attend", "take", "do", "make", "get", "participate",
})

# ── BOOTSTRAP INCHOATIVE / aspectual START-verb set — DB-DOWN SAFETY NET ONLY ────────
# The small grammatical class of INCHOATIVE / ingressive verbs — verbs whose lexical aspect marks the
# BEGINNING of an activity or process ("started the seeds", "began piano lessons", "launched the
# project", "took up running"). This is a LEXICAL-ASPECT grammatical class (the ingressive verbs),
# NOT a domain/event word-list — exactly like the light/support-verb class above. Membership is
# corroborated downstream by the parse: the verb must directly govern a concrete DIRECT OBJECT (the
# thing being started) with a 1st-person subject, and the clause must carry a DATE — so a non-eventive
# use ("I started to think", "I started crying") never yields a dated occurrence. Hard-coded so a
# pre-migration / DB-down turn still recognizes the ingressive construction instead of silently
# dropping the dated start. DB-HELD + per-tenant + GROWABLE on the SAME rail (category=
# 'inchoative_verb'); this in-code set is the DB-DOWN code-fallback seed only, NOT the authority.
_BOOTSTRAP_INCHOATIVE_VERBS: frozenset[str] = frozenset({
    "start", "begin", "commence", "launch", "initiate", "undertake",
})

# ── BOOTSTRAP ASPECTUAL / PHASE CONTROL-verb set — DB-DOWN SAFETY NET ONLY ───────────
# The bounded ASPECTUAL (phase) verb class that, as a SUBJECT-CONTROL matrix, raises the subject and
# leaves the REALIZED activity in a progressive ``-ing`` ``xcomp`` ("I STARTED working with Rachel",
# "I KEPT emailing Tom", "I CONTINUED reviewing the report"). Used by
# ``linguistics._aspectual_activity_xcomp`` to license DESCENDING into that xcomp so the split SVO
# (subject on the matrix, object on the activity verb) still mints (user, work_with, rachel). This is
# a LEXICAL-ASPECT (ingressive + continuative + terminative phase) primitive — start/begin/keep/
# continue/resume/finish/stop — NOT a domain/event word-list and NOT the open verb class itself; the
# descent is further firewalled at the call site (progressive -ing xcomp + NOT catenative/mental-state)
# so an UNREALIZED intent ("started to think", "want to buy", "considered hiring") never descends.
# DELIBERATELY DISTINCT from inchoative_verb: the inchoative rail feeds ``analyze_inchoative`` (a NOUN-
# object "started <item>" occurrence), where adding continuative/terminative phase verbs (keep/continue)
# would mis-mint "I kept the receipt" → an occurrence. Same rail/machinery, separate aspectual category.
# DB-HELD + per-tenant + GROWABLE on the SAME rail (category='aspectual_control_verb'); this in-code
# set is the DB-DOWN code-fallback seed only, NOT the authority.
_BOOTSTRAP_ASPECTUAL_CONTROL_VERBS: frozenset[str] = frozenset({
    "start", "begin", "continue", "keep", "resume", "commence", "finish", "stop",
})

# ── BOOTSTRAP ACQUISITION / TRANSFER-OF-POSSESSION verb set — DB-DOWN SAFETY NET ONLY ─
# The bounded LEXICAL class of TRANSFER-OF-POSSESSION verbs: a verb whose lexical semantics is the
# subject COMING TO POSSESS its direct object ("I GOT a phone", "I BOUGHT a laptop", "I ACQUIRED a
# car", "I RECEIVED a gift", "I PURCHASED a tablet", "I OBTAINED a licence"). This is the change-of-
# possession counterpart of the inchoative (change-of-state) class — a lexical-semantic primitive, NOT
# a domain/product word-list. ⚠️ FLAGGED BOUNDED CLASS (per the Q4 brief): the acquisition signal
# cannot be made purely structural — "got a phone" and "had a meeting" are the SAME light-verb dep
# shape (verb→dobj NOUN/PROPN); only the verb's lexical semantics distinguishes COMING-TO-POSSESS from
# a light-verb occurrence. So a small bounded verb class is unavoidable here, EXACTLY as for the
# naming / LVC / inchoative / aspectual classes. It is firewalled downstream by the parse the SAME way
# the others are (1st-person subject + a CONCRETE direct object that becomes the possession; a verb-
# complement xcomp "I got to leave" / an eventive-noun dobj "I got a haircut" is excluded by POS +
# the possession-object discipline), and it is DB-HELD + per-tenant + GROWABLE on the SAME rail
# (category='acquisition_verb') so a tenant grows its own transfer verbs (freq-gated) without code
# edits. This in-code set is the DB-DOWN code-fallback seed only, NOT the authority.
_BOOTSTRAP_ACQUISITION_VERBS: frozenset[str] = frozenset({
    "get", "buy", "purchase", "acquire", "obtain", "receive", "grab", "pick",
})

# ── BOOTSTRAP PROBLEM-NOUN (bland eventive head) set — DB-DOWN SAFETY NET ONLY ───────
# The bounded LEXICAL class of SEMANTICALLY-EMPTY PROBLEM / FAULT eventive heads — the bland nouns
# that head an LVC "I had an <issue/problem/trouble> with X" construction where the MEANING lives in
# the with-PP affected entity ("X"), not the empty head. Recognizing the head as a problem_noun (AND
# a with-PP affected entity being present) lets the deriver emit a competing `(<affected>, has_state,
# <problem-state>)` candidate — the affected entity's PROBLEM as a typed reusable state node (the
# structural twin of `feels`). ⚠️ FLAGGED BOUNDED CLASS (the GPS / Stage-3 brief): the problem reading
# cannot be made purely structural — "had an issue with X" and "had a meeting with X" share the SAME
# dep shape (light-verb → dobj NOUN → with-PP); only the HEAD NOUN's lexical semantics distinguishes a
# problem/fault state from a neutral occurrence. So a small bounded NOUN class is unavoidable here,
# EXACTLY as for the naming / LVC / inchoative / aspectual / acquisition classes. It is firewalled
# downstream by the parse the SAME way: the state reading fires ONLY when the head is in THIS class AND
# a with-PP supplies the affected entity — a non-problem "have + with-PP" ("had a meeting with Taylor",
# "had lunch with Tom", "had a call with the team") is UNTOUCHED (head ∉ problem_noun). DB-HELD + per-
# tenant + GROWABLE on the SAME rail (category='problem_noun'); this in-code set is the DB-DOWN code-
# fallback seed only, NOT the authority. Mirrors migration 116's public seed.
_BOOTSTRAP_PROBLEM_NOUNS: frozenset[str] = frozenset({
    "issue", "problem", "trouble", "fault", "difficulty",
    "glitch", "bug", "error", "concern",
})

# ── BOOTSTRAP load-bearing SVO particle set — DB-DOWN SAFETY NET ONLY ────────────────
# The closed grammatical class of particles/prepositions that are LOAD-BEARING on a verb (they change
# the relation: "go" vs "go to", "work" vs "work for", "move" vs "move into"). Kept on the predicate
# token; everything else after the verb is the object/scalar tail. A language primitive (the ADP/PART
# surface forms a verb governs), aligned with predicate_span._KEEP_PREPOSITIONS — NOT a domain list.
# Hard-coded so a pre-migration / DB-down turn still keeps the load-bearing particle on the predicate.
# Mirrors the retired in-code `linguistics._SVO_KEEP_PARTICLES`.
_BOOTSTRAP_SVO_PARTICLES: frozenset[str] = frozenset({
    "to", "for", "with", "in", "on", "at", "from", "into", "about", "of",
})

# ── BOOTSTRAP DISCOURSE-MARKER set — DB-DOWN SAFETY NET ONLY ─────────────────────────
# The closed pragmatic class of sentence-initial discourse markers ("by the way", "anyway",
# "actually") that introduce an aside and must NEVER seed a fact ("by the way" must not yield
# (i, have, way)). A language/pragmatics primitive, NOT a domain list. DB-HELD + per-tenant + GROWABLE
# on the SAME rail (category='discourse_marker'); this in-code set is the DB-DOWN code-fallback seed.
_BOOTSTRAP_DISCOURSE_MARKERS: frozenset[str] = frozenset({
    "by the way", "anyway", "anyways", "actually", "honestly", "frankly",
    "to be honest", "in any case", "incidentally", "for what it's worth",
    "as it happens", "speaking of which", "that said", "on another note",
})

# ── BOOTSTRAP RELATIONAL-NOUN set — DB-DOWN SAFETY NET ONLY ──────────────────────────
# The (open-ended, growable) class of RELATIONAL / component / kinship nouns: a noun whose meaning is
# INHERENTLY a relation to a whole or a person ("X's gps" → a component of X; "X's mother" → a kinship
# of X), as opposed to a SORTAL noun whose meaning is a free-standing kind ("X's book"). This is the
# research-backed relational-vs-sortal split (Löbner; Barker's relational nouns) that the genitive
# possessive deriver uses to pick the inherent relation (part_of / has_component / kinship) over a
# generic ``related_to``. It is DB-HELD + per-tenant + GROWABLE on the SAME rail as the verb cue
# classes; this in-code set is the DB-DOWN code-fallback seed only (evidenced common component/kinship
# nouns), NOT the authority. A genitive over a noun OUTSIDE this set falls to generic ``related_to``,
# so a miss never fabricates a wrong relation — it just stays generic and the walk resolves it.
_BOOTSTRAP_RELATIONAL_NOUNS: frozenset[str] = frozenset({
    # component / part nouns (mereological)
    "gps", "engine", "sail", "leg", "wheel", "screen", "battery", "keyboard", "tire",
    "door", "roof", "handle", "blade", "edge", "surface", "side", "top", "bottom",
    "component", "part", "piece", "system", "module", "port", "cable",
    # kinship / social-relational nouns
    "mother", "father", "mom", "dad", "parent", "sister", "brother", "sibling",
    "son", "daughter", "child", "wife", "husband", "spouse", "partner", "friend",
    "uncle", "aunt", "cousin", "grandmother", "grandfather", "grandma", "grandpa",
    "boss", "manager", "colleague", "neighbour", "neighbor", "owner",
    # body-part nouns (anatomical mereology)
    "arm", "hand", "foot", "head", "eye", "ear", "nose", "heart", "back", "knee",
})

# ── BOOTSTRAP KINSHIP-NOUN set — DB-DOWN SAFETY NET ONLY ─────────────────────────────
# The (growable) closed-ish class of KINSHIP / social-relational nouns — the relational nouns whose
# inherent relation is a person↔person link (kinship) rather than a component/mereology link. The
# genitive-possessive deriver, having ALREADY confirmed a noun is in the `relational_noun` class,
# consults this set to pick the inherent relation: in this set → kinship (``related_to``, the
# resolver/ontology grounds the specific kin rel_type downstream); NOT in this set → component/part
# mereology (``part_of``). A noun OUTSIDE the relational_noun class never reaches here. DB-HELD +
# per-tenant + GROWABLE on the SAME rail (category='kinship_noun'); this in-code set is the DB-DOWN
# code-fallback seed only — the EXACT contents of the retired in-code `_KINSHIP_RELATIONAL_NOUNS`.
_BOOTSTRAP_KINSHIP_NOUNS: frozenset[str] = frozenset({
    "mother", "father", "mom", "dad", "parent", "sister", "brother", "sibling",
    "son", "daughter", "child", "wife", "husband", "spouse", "partner",
    "uncle", "aunt", "cousin", "grandmother", "grandfather", "grandma", "grandpa",
})

# ── BOOTSTRAP KINSHIP-NOUN → REL_TYPE MAP — DB-DOWN SAFETY NET ONLY ──────────────────
# A MAP (kinship noun lemma → the rel_type the HEAD noun plays toward the POSSESSOR), NOT a set:
# "my mother" → mother is the PARENT of me → parent_of; "my son" → son is the CHILD of me → child_of;
# "my wife/husband/spouse/partner" → spouse; "my sister/brother/sibling" → sibling_of. A kin with no
# exact 1-hop rel_type (grandparent / uncle / aunt / cousin) maps to the generic ``related_to`` — the
# walk/ontology grounds the specific kin downstream, we never fabricate a wrong direct rel. Stored on
# the SAME (cue, category) rail as thin_type: `cue` = the kinship noun, `description` = the rel_type.
# This in-code map is the DB-DOWN code-fallback seed only — mirrors migration 109's public seed.
_BOOTSTRAP_KINSHIP_REL_MAP: dict[str, str] = {
    "mother": "parent_of", "father": "parent_of", "mom": "parent_of",
    "dad": "parent_of", "parent": "parent_of",
    "sister": "sibling_of", "brother": "sibling_of", "sibling": "sibling_of",
    "son": "child_of", "daughter": "child_of", "child": "child_of",
    "wife": "spouse", "husband": "spouse", "spouse": "spouse", "partner": "spouse",
    "uncle": "related_to", "aunt": "related_to", "cousin": "related_to",
    "grandmother": "related_to", "grandfather": "related_to",
    "grandma": "related_to", "grandpa": "related_to",
}

# ── BOOTSTRAP MEASUREMENT-UNIT → SCALAR REL_TYPE MAP — DB-DOWN SAFETY NET ONLY ───────
# A MAP (measurement-unit head lemma → the SCALAR rel_type it measures) for the copula measurement
# chain: "she is 62 years old" → unit "year" → age; "he is 6 feet tall" → unit "foot" → height; "it
# weighs 80 kilograms" → unit "kilogram" → weight. These rel_types carry tail_types={SCALAR} so the
# value routes to entity_attributes. The bare-age fallback ("Taylor is 28" — a NUM attr with no unit)
# resolves to `age` via the deriver's grammatical age-shape, NOT this map. A unit OUTSIDE this map →
# no scalar minted (we never guess a measurement). Stored on the SAME (cue, category) rail: `cue` =
# the unit lemma, `description` = the scalar rel_type. DB-DOWN code-fallback seed only.
_BOOTSTRAP_UNIT_SCALAR_MAP: dict[str, str] = {
    "year": "age",
    "foot": "height", "feet": "height", "inch": "height",
    "centimetre": "height", "centimeter": "height", "cm": "height", "metre": "height", "meter": "height",
    "pound": "weight", "lb": "weight", "kilogram": "weight", "kg": "weight", "kilo": "weight",
}

# ── BOOTSTRAP THIN-TYPE MAP — DB-DOWN SAFETY NET ONLY ────────────────────────────────
# A MAP (surface head lemma → coarse one-step "thin" slot-type tag), NOT a set: a few high-signal
# device/system compound heads collapse to their immediate kind ("gps system"/"system" → "device").
# This is ONLY a slot tag the deriver stamps — GLiNER2 supplies real typing and WINS over it
# (`_thin_type_label_for_object`, monotonic-forward authority). Modeled on the SAME (cue, category)
# rail by storing the SURFACE in `cue` and the TARGET TYPE in `description`, so the keyed value rides
# the existing table without a new schema; `resolve_thin_type()` returns the {surface: type} dict.
# This in-code map is the DB-DOWN code-fallback seed only — the EXACT contents of the retired in-code
# `_thin_type_for_token` dict.
_BOOTSTRAP_THIN_TYPE_MAP: dict[str, str] = {
    "system": "device",
    "device": "device",
    "gadget": "device",
    "appliance": "device",
    "machine": "device",
}

# Cue CATEGORIES this module resolves. The table is general by category so every verb/particle cue
# class rides the SAME rail (one table, one overlay) without a new module.
NAMING_VERB_CATEGORY = "naming_verb"
LVC_SUPPORT_VERB_CATEGORY = "lvc_support_verb"
INCHOATIVE_VERB_CATEGORY = "inchoative_verb"
ASPECTUAL_CONTROL_VERB_CATEGORY = "aspectual_control_verb"
ACQUISITION_VERB_CATEGORY = "acquisition_verb"
PROBLEM_NOUN_CATEGORY = "problem_noun"
SVO_PARTICLE_CATEGORY = "svo_particle"
RELATIONAL_NOUN_CATEGORY = "relational_noun"
DISCOURSE_MARKER_CATEGORY = "discourse_marker"
KINSHIP_NOUN_CATEGORY = "kinship_noun"
# Thin-type is a KEYED-VALUE class (surface→type) on the SAME rail (cue=surface, description=type),
# resolved by resolve_thin_type() into a dict — not by the set-returning resolve_cues path.
THIN_TYPE_CATEGORY = "thin_type"
# The kinship_noun rows ALSO carry a KEYED VALUE (noun→rel_type) in `description`, resolved by
# resolve_kinship_rel_map() into a {noun: rel_type} dict (same rail, same rows as the kinship_noun
# SET — the SET is resolve_kinship_nouns, the MAP is resolve_kinship_rel_map). No separate category.
# unit_scalar is its OWN keyed class (unit-lemma → scalar rel_type) for the copula measurement chain.
UNIT_SCALAR_CATEGORY = "unit_scalar"

# Per-category DB-DOWN fallback seed. resolve_cues consults this when a category resolves empty / the
# read fails, so EVERY category fails safe to its own evidenced floor (never the wrong class, never
# empty). naming_verb keeps its dedicated bootstrap for back-compat with resolve_naming_verbs.
_BOOTSTRAP_BY_CATEGORY: dict[str, frozenset[str]] = {
    NAMING_VERB_CATEGORY: _BOOTSTRAP_NAMING_VERBS,
    LVC_SUPPORT_VERB_CATEGORY: _BOOTSTRAP_LVC_SUPPORT_VERBS,
    INCHOATIVE_VERB_CATEGORY: _BOOTSTRAP_INCHOATIVE_VERBS,
    ASPECTUAL_CONTROL_VERB_CATEGORY: _BOOTSTRAP_ASPECTUAL_CONTROL_VERBS,
    ACQUISITION_VERB_CATEGORY: _BOOTSTRAP_ACQUISITION_VERBS,
    PROBLEM_NOUN_CATEGORY: _BOOTSTRAP_PROBLEM_NOUNS,
    SVO_PARTICLE_CATEGORY: _BOOTSTRAP_SVO_PARTICLES,
    RELATIONAL_NOUN_CATEGORY: _BOOTSTRAP_RELATIONAL_NOUNS,
    DISCOURSE_MARKER_CATEGORY: _BOOTSTRAP_DISCOURSE_MARKERS,
    KINSHIP_NOUN_CATEGORY: _BOOTSTRAP_KINSHIP_NOUNS,
    # THIN_TYPE_CATEGORY is intentionally NOT here: it is a keyed-value (surface→type) class resolved
    # by resolve_thin_type() into a dict, not a flat cue set. Its DB-DOWN fallback is
    # _BOOTSTRAP_THIN_TYPE_MAP, applied in resolve_thin_type().
}


def _bootstrap_for(category: str) -> frozenset[str]:
    """The DB-DOWN code-fallback seed for `category` (never empty). Unknown category → naming seed
    (back-compat default); the three known classes return their own evidenced floor."""
    return _BOOTSTRAP_BY_CATEGORY.get(category, _BOOTSTRAP_NAMING_VERBS)


def _fetch_cues(dsn: str, schema_qualifier: str, category: str) -> frozenset[str]:
    """Read ACTIVE cue lemmas of `category` from a single explicit schema. `schema_qualifier` is a
    bare, already-validated schema identifier ('public' or 'faultline_<slug>'). Returns a frozenset
    of lowercased cue lemmas. Raises on a missing table / read error so the caller's fail-safe
    (bootstrap) applies."""
    cues: set[str] = set()
    # connect_timeout (CONNECTION guard, NOT an LLM/op timeout): a momentarily-slow PG must not block
    # a turn unboundedly on a cold cue read. On timeout/failure psycopg2 raises → the caller's
    # fail-safe (bootstrap cue set) applies; correctness is preserved.
    with psycopg2.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT cue FROM {schema_qualifier}.linguistic_cues "
                f"WHERE category = %s AND is_active = true",
                (category,),
            )
            for (cue,) in cur.fetchall():
                if cue and cue.strip():
                    cues.add(cue.strip().lower())
    return frozenset(cues)


def _get_seed(dsn: str, category: str) -> frozenset[str]:
    """Return the cached public.linguistic_cues set (TTL-refreshed) for the UNSCOPED fallback path
    ONLY (no tenant bound). Returns the BOOTSTRAP set if public is unreadable / empty. Callers must
    NOT mutate the returned set (it is a frozenset)."""
    now = time.time()
    with _lock:
        entry = _seed_cache.get(category)
        if entry and entry["cues"] and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return entry["cues"]
    try:
        fresh = _fetch_cues(dsn, "public", category)
        if not fresh:
            fresh = _bootstrap_for(category)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistic_cue_overlay.seed_fetch_failed", category=category, error=str(e)[:160])
        with _lock:
            cached = _seed_cache.get(category)
            return (cached["cues"] if cached and cached["cues"] else _bootstrap_for(category))
    with _lock:
        _seed_cache[category] = {"cues": fresh, "loaded_at": time.time()}
        return fresh


def _is_real_tenant_schema(schema_name) -> bool:
    if not schema_name:
        return False
    s = schema_name.strip().lower()
    return s not in ("", "public")


def resolve_cues(dsn: str, schema_name, category: str = NAMING_VERB_CATEGORY) -> frozenset[str]:
    """Resolve the ACTIVE cue-lemma set of `category` from the BOUND TENANT SCHEMA ONLY.

    Returns a frozenset of lowercased cue lemmas (do NOT mutate). Cache hit performs no DB query.

    schema_name None / "public" → unscoped fallback: read the public template.
    schema_name = real tenant   → read `<schema>.linguistic_cues` ONLY (seed-copied ∪ grown).
        If the tenant schema is unreadable / the table is missing (pre-migration) we FAIL SAFE to the
        BOOTSTRAP set — we do NOT read public for a bound tenant (isolation) and we NEVER return an
        empty set (that would silently drop naming detection).
    """
    if not dsn:
        return _bootstrap_for(category)

    if not _is_real_tenant_schema(schema_name):
        return _get_seed(dsn, category)

    schema_name = schema_name.strip()
    cache_key = f"{schema_name}::{category}"
    now = time.time()

    with _lock:
        entry = _overlay_cache.get(cache_key)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return entry["cues"]

    try:
        tenant_cues = _fetch_cues(dsn, schema_name, category)
        if not tenant_cues:
            # Table present but this category empty (mis-seeded / pre-migration category) → the
            # category's own bootstrap so detection never silently drops.
            tenant_cues = _bootstrap_for(category)
    except Exception as e:  # noqa: BLE001
        # Tenant schema unreadable / table missing (pre-migration). FAIL SAFE to bootstrap; do NOT
        # read public for a bound tenant (would mask the failure / cross isolation).
        log.warning("linguistic_cue_overlay.tenant_fetch_failed",
                    schema=schema_name, category=category, error=str(e)[:160])
        return _bootstrap_for(category)

    with _lock:
        _overlay_cache[cache_key] = {"cues": tenant_cues, "loaded_at": time.time()}
    return tenant_cues


def resolve_naming_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE NAMING-verb lemma set for the ContextVar-bound current request
    schema (tenant-only). Uses the SAME binding as the rel_type / taxonomy / temporal resolvers.
    Fail-safe: never empty (bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), NAMING_VERB_CATEGORY)


def resolve_current(dsn: str) -> frozenset[str]:
    """Alias for `resolve_naming_verbs` mirroring the sibling overlays' `resolve_current` contract."""
    return resolve_naming_verbs(dsn)


def resolve_lvc_support_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE LIGHT/SUPPORT-verb (LVC) lemma set for the ContextVar-bound
    current request schema (tenant-only), via the SAME binding as the naming/rel_type/temporal
    resolvers. Fail-safe: never empty (the lvc_support_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), LVC_SUPPORT_VERB_CATEGORY)


def resolve_inchoative_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE INCHOATIVE / ingressive START-verb lemma set for the ContextVar-
    bound current request schema (tenant-only), via the SAME binding as the naming/lvc/temporal
    resolvers. Used by ``linguistics.analyze_inchoative`` to recognize a dated "started <item>"
    occurrence. Fail-safe: never empty (the inchoative_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), INCHOATIVE_VERB_CATEGORY)


def resolve_aspectual_control_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE ASPECTUAL / phase SUBJECT-CONTROL verb lemma set for the
    ContextVar-bound current request schema (tenant-only), via the SAME binding as the naming/lvc/
    inchoative/temporal resolvers. Used by ``linguistics._aspectual_activity_xcomp`` to license
    descending into a progressive ``-ing`` activity ``xcomp`` ("I started working with Rachel").
    DELIBERATELY DISTINCT from the inchoative set (see ``_BOOTSTRAP_ASPECTUAL_CONTROL_VERBS``).
    Fail-safe: never empty (the aspectual_control_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), ASPECTUAL_CONTROL_VERB_CATEGORY)


def resolve_acquisition_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE ACQUISITION / transfer-of-possession verb lemma set for the
    ContextVar-bound current request schema (tenant-only), via the SAME binding as the naming/lvc/
    inchoative/temporal resolvers. Used by ``linguistics.analyze_acquisition`` to recognize a dated
    "got/bought a <device>" coming-to-possess construction so the user→device ownership linkage is
    EXPOSED as an inferred, dated edge. ⚠️ FLAGGED bounded lexical class (see
    ``_BOOTSTRAP_ACQUISITION_VERBS``). Fail-safe: never empty (the acquisition_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), ACQUISITION_VERB_CATEGORY)


def resolve_problem_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE PROBLEM-NOUN (bland eventive head) lemma set for the ContextVar-
    bound current request schema (tenant-only), via the SAME binding as the naming/lvc/acquisition/
    temporal resolvers. Used by ``linguistics.analyze_events`` (the with-PP state lane) to recognize a
    SEMANTICALLY-EMPTY problem head ("had an issue/problem/trouble WITH X") so a competing
    ``(<affected>, has_state, <problem-state>)`` candidate is emitted alongside the participated_in
    candidate (Stage-2 arbitration picks the strong state reading). ⚠️ FLAGGED bounded lexical class
    (see ``_BOOTSTRAP_PROBLEM_NOUNS``). Fail-safe: never empty (the problem_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), PROBLEM_NOUN_CATEGORY)


def resolve_svo_particles(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE load-bearing SVO-particle set for the ContextVar-bound current
    request schema (tenant-only), via the SAME binding as the naming/rel_type/temporal resolvers.
    Fail-safe: never empty (the svo_particle bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), SVO_PARTICLE_CATEGORY)


def resolve_relational_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE RELATIONAL-noun set for the ContextVar-bound current request
    schema (tenant-only), via the SAME binding as the naming/rel_type/temporal resolvers. Used by the
    genitive-possessive deriver to split relational/component/kinship nouns (inherent relation) from
    sortal nouns (generic related_to). Fail-safe: never empty (the relational_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), RELATIONAL_NOUN_CATEGORY)


def resolve_kinship_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE KINSHIP-noun set for the ContextVar-bound current request schema
    (tenant-only), via the SAME binding as the naming/rel_type/temporal resolvers. Used by the
    genitive-possessive deriver's inherent-relation pick: a relational noun IN this set is a
    person↔person kinship link (``related_to``); NOT in it is component/part mereology (``part_of``).
    Fail-safe: never empty (the kinship_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), KINSHIP_NOUN_CATEGORY)


def _fetch_thin_type_map(dsn: str, schema_qualifier: str) -> dict[str, str]:
    """Read the ACTIVE thin-type (surface→type) MAP from a single explicit schema. Mirrors
    `_fetch_cues` but returns a {surface: type} dict: `cue` is the surface head lemma, `description`
    is the coarse target type. `schema_qualifier` is a bare, already-validated schema identifier
    ('public' or 'faultline_<slug>'). Raises on a missing table / read error so the caller's
    fail-safe (the bootstrap map) applies. A row with an empty/NULL description is skipped (a thin
    type with no target carries no slot tag)."""
    out: dict[str, str] = {}
    with psycopg2.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT cue, description FROM {schema_qualifier}.linguistic_cues "
                f"WHERE category = %s AND is_active = true",
                (THIN_TYPE_CATEGORY,),
            )
            for (cue, desc) in cur.fetchall():
                if cue and cue.strip() and desc and desc.strip():
                    out[cue.strip().lower()] = desc.strip().lower()
    return out


# Per-schema cache for the keyed thin-type MAP (separate from the set cache _overlay_cache because the
# value shape differs): {cache_key: {"map": dict[str,str], "loaded_at": float}}.
_thin_type_cache: dict[str, dict] = {}
_thin_type_seed_cache: dict = {}


def resolve_thin_type(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE thin-type (surface→coarse-type) MAP for the ContextVar-bound
    current request schema. Returns a {surface_lemma: type} dict (do NOT mutate). Same ContextVar
    binding / TTL / per-tenant isolation / fail-safe contract as `resolve_cues`, but the value is a
    KEYED MAP (cue→description) instead of a flat set.

    schema None / "public" → unscoped fallback: read the public template (or bootstrap if unreadable).
    real tenant            → read `<schema>.linguistic_cues` category='thin_type' ONLY. Unreadable /
        missing table (pre-migration) / empty → FAIL SAFE to `_BOOTSTRAP_THIN_TYPE_MAP`; never read
        public for a bound tenant (isolation); never return empty (would silently drop the slot tag).
    """
    schema_name = rel_type_overlay.get_current_schema()
    if not dsn:
        return dict(_BOOTSTRAP_THIN_TYPE_MAP)

    # Unscoped fallback (boot / anonymous): read the public template, cache with TTL.
    if not _is_real_tenant_schema(schema_name):
        now = time.time()
        with _lock:
            entry = _thin_type_seed_cache.get("public")
            if entry and entry["map"] and (now - entry["loaded_at"]) <= _TTL_SECONDS:
                return entry["map"]
        try:
            fresh = _fetch_thin_type_map(dsn, "public")
            if not fresh:
                fresh = dict(_BOOTSTRAP_THIN_TYPE_MAP)
        except Exception as e:  # noqa: BLE001 — fail-safe
            log.warning("linguistic_cue_overlay.thin_type_seed_fetch_failed", error=str(e)[:160])
            with _lock:
                cached = _thin_type_seed_cache.get("public")
                return (cached["map"] if cached and cached["map"] else dict(_BOOTSTRAP_THIN_TYPE_MAP))
        with _lock:
            _thin_type_seed_cache["public"] = {"map": fresh, "loaded_at": time.time()}
            return fresh

    schema_name = schema_name.strip()
    cache_key = f"{schema_name}::{THIN_TYPE_CATEGORY}"
    now = time.time()
    with _lock:
        entry = _thin_type_cache.get(cache_key)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return entry["map"]
    try:
        tenant_map = _fetch_thin_type_map(dsn, schema_name)
        if not tenant_map:
            tenant_map = dict(_BOOTSTRAP_THIN_TYPE_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe; do NOT read public for a bound tenant
        log.warning("linguistic_cue_overlay.thin_type_tenant_fetch_failed",
                    schema=schema_name, error=str(e)[:160])
        return dict(_BOOTSTRAP_THIN_TYPE_MAP)
    with _lock:
        _thin_type_cache[cache_key] = {"map": tenant_map, "loaded_at": time.time()}
    return tenant_map


# Per-schema cache for the GENERIC keyed maps (kinship_rel, unit_scalar). Keyed by
# "<schema>::<category>" so each keyed class has its own slot. Same shape as _thin_type_cache.
_keyed_map_cache: dict[str, dict] = {}
_keyed_map_seed_cache: dict[str, dict] = {}


def _resolve_keyed_map(dsn: str, category: str, bootstrap: dict[str, str]) -> dict[str, str]:
    """Resolve a per-tenant ACTIVE keyed (cue→description) MAP for `category` on the ContextVar-bound
    current request schema. Mirrors `resolve_thin_type` exactly (same TTL / per-tenant isolation /
    fail-safe contract) but is GENERIC over the category + its DB-DOWN bootstrap map, so kinship_rel
    and unit_scalar (and any future keyed class) share ONE implementation. Returns a {cue: value}
    dict (do NOT mutate). Never reads public for a bound tenant; never returns empty (bootstrap floor).
    """
    schema_name = rel_type_overlay.get_current_schema()
    if not dsn:
        return dict(bootstrap)
    # Unscoped fallback (boot / anonymous): read the public template, cache with TTL.
    if not _is_real_tenant_schema(schema_name):
        now = time.time()
        seed_key = f"public::{category}"
        with _lock:
            entry = _keyed_map_seed_cache.get(seed_key)
            if entry and entry["map"] and (now - entry["loaded_at"]) <= _TTL_SECONDS:
                return entry["map"]
        try:
            fresh = _fetch_keyed_map(dsn, "public", category)
            if not fresh:
                fresh = dict(bootstrap)
        except Exception as e:  # noqa: BLE001 — fail-safe
            log.warning("linguistic_cue_overlay.keyed_map_seed_fetch_failed",
                        category=category, error=str(e)[:160])
            with _lock:
                cached = _keyed_map_seed_cache.get(seed_key)
                return (cached["map"] if cached and cached["map"] else dict(bootstrap))
        with _lock:
            _keyed_map_seed_cache[seed_key] = {"map": fresh, "loaded_at": time.time()}
            return fresh

    schema_name = schema_name.strip()
    cache_key = f"{schema_name}::{category}"
    now = time.time()
    with _lock:
        entry = _keyed_map_cache.get(cache_key)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return entry["map"]
    try:
        tenant_map = _fetch_keyed_map(dsn, schema_name, category)
        if not tenant_map:
            tenant_map = dict(bootstrap)
    except Exception as e:  # noqa: BLE001 — fail-safe; do NOT read public for a bound tenant
        log.warning("linguistic_cue_overlay.keyed_map_tenant_fetch_failed",
                    schema=schema_name, category=category, error=str(e)[:160])
        return dict(bootstrap)
    with _lock:
        _keyed_map_cache[cache_key] = {"map": tenant_map, "loaded_at": time.time()}
    return tenant_map


def _fetch_keyed_map(dsn: str, schema_qualifier: str, category: str) -> dict[str, str]:
    """Read the ACTIVE (cue→description) MAP of `category` from a single explicit schema. Mirrors
    `_fetch_thin_type_map` but is category-parameterized. Raises on a missing table / read error so the
    caller's fail-safe applies. A row with an empty/NULL description is skipped (no mapping)."""
    out: dict[str, str] = {}
    with psycopg2.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT cue, description FROM {schema_qualifier}.linguistic_cues "
                f"WHERE category = %s AND is_active = true",
                (category,),
            )
            for (cue, desc) in cur.fetchall():
                if cue and cue.strip() and desc and desc.strip():
                    out[cue.strip().lower()] = desc.strip().lower()
    return out


def resolve_kinship_rel_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE kinship-noun → rel_type MAP for the ContextVar-bound current
    request schema. Reads the `description` column of the kinship_noun rows ({noun: rel_type}). Used
    by the genitive/possessive deriver's inherent-relation pick so the SPECIFIC kin rel (parent_of /
    child_of / sibling_of / spouse / related_to) is metadata-driven, NOT an in-code literal. Same
    contract as resolve_thin_type. Fail-safe: bootstrap floor (`_BOOTSTRAP_KINSHIP_REL_MAP`)."""
    return _resolve_keyed_map(dsn, KINSHIP_NOUN_CATEGORY, _BOOTSTRAP_KINSHIP_REL_MAP)


def resolve_unit_scalar_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE measurement-unit → scalar rel_type MAP for the ContextVar-bound
    current request schema. Reads the unit_scalar rows ({unit: rel_type}). Used by the copula
    measurement chain so "she is 62 years old" → unit 'year' → age (a SCALAR rel routed to
    entity_attributes). Same contract as resolve_thin_type. Fail-safe: bootstrap floor
    (`_BOOTSTRAP_UNIT_SCALAR_MAP`)."""
    return _resolve_keyed_map(dsn, UNIT_SCALAR_CATEGORY, _BOOTSTRAP_UNIT_SCALAR_MAP)


def invalidate(schema_name=None) -> None:
    """Invalidate caches.

    schema_name given  → drop that tenant's cache (next read rebuilds it). What a grown-cue approval /
                          refresh calls so only that tenant's cache is rebuilt.
    schema_name None   → drop ALL per-tenant caches AND the unscoped public-template fallback cache
                          (full reset).
    """
    with _lock:
        if _is_real_tenant_schema(schema_name):
            prefix = f"{schema_name.strip()}::"
            for k in [k for k in _overlay_cache if k.startswith(prefix)]:
                _overlay_cache.pop(k, None)
            for k in [k for k in _thin_type_cache if k.startswith(prefix)]:
                _thin_type_cache.pop(k, None)
            for k in [k for k in _keyed_map_cache if k.startswith(prefix)]:
                _keyed_map_cache.pop(k, None)
        else:
            _overlay_cache.clear()
            _seed_cache.clear()
            _thin_type_cache.clear()
            _thin_type_seed_cache.clear()
            _keyed_map_cache.clear()
            _keyed_map_seed_cache.clear()
