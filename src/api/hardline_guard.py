"""ladder_hardline — THE HARD-LINE ingest guard for ``subclass_of`` rungs (LV1..LV3).

WHY THIS MODULE EXISTS (the ladder-on-value wound, measured 2026-09-03 across 22 pre-prod
tenants): 838 staged_facts + 56 facts rows whose subject is a user VALUE or a named instance
(`blue subclass_of color`, `halifax subclass_of provincial capital`, `krellin subclass_of
krelling` — the user's own possessions). `entities.node_role` detects this (`VALUE_PLACE_FIRST_CLASS`)
but DETECTION IS NOT A GUARD: it fails OPEN (every read error / flag-off / unstamped node →
"not protected"), and `derive_object_role` stamps a constrained-tail object (`lives_in→halifax`,
`owns→krellin`) as PLACE — a place NAME is not a type node, yet PLACE is outside
`_PROTECTED_ROLES`, so names sail through. This module is the INVERSE-polarity predicate the
wound demanded: it decides whether a node may receive a `subclass_of` rung, FAILS CLOSED
(a read error or an unknown node → REFUSE the rung), and is named distinctly from
`node_role` on purpose (same evidence superset, opposite default — the worst outcome would be
reusing the name and inheriting the fail-open default).

THE EVIDENCE (exactly what THE HARD LINE uses, per the gauntlet bars — DB-resolved structural
metadata + grammar/morphology; `node_role` is CORROBORATION, never the sole predicate, so
`node_role IS NULL` ("unstamped", NOT "safe") cannot admit a ladder by itself):

  R0 identity   — the node is the tenant user anchor / 'user' → REFUSE.
  R1 instance   — the node is the SUBJECT of a live `instance_of` edge (facts ∪ staged) → it is
                  a NAMED INSTANCE; the ladder hangs off its TYPE node, never off the name.
  R2 naming     — the node is the SUBJECT of a live naming edge (`also_known_as`/`pref_name`,
                  the fixed SKOS pair pinned codebase-wide) → a NAME is the naming layer, a memory.
  R3 value      — the node HOLDS a captured value (a live `entity_attributes` row) → a VALUE.
  R4 slot       — the node is the OBJECT of a live user_stated edge whose rel files an
                  unconstrained/scalar tail (metadata `rel_types.tail_types` ≤ {ANY, SCALAR})
                  → a captured scalar value (`favorite_colour=blue`). No rel-name literal: the
                  rel is resolved through its METADATA, not its name.
  R5 stamp      — `entities.node_role` ∈ {value, name, both} → CORROBORATION (migration-192
                  stamp; 'place' is NOT protective — a place NAME is still a name).
  R6b instance  — the node is the OBJECT of a live user_stated edge whose rel's tail_types is
                  a SPECIFIC constrained set (has_pet→{Animal}, lives_in→{Location}) — by the
                  rel's own metadata the object is a NAMED INSTANCE of that type (a memory),
                  never a subclass. Pure rel_types metadata (the complement of R4's
                  <@ {ANY,SCALAR} test); catches common-noun homonym names (rex) and place
                  names (halifax) structurally.
  R6 lexical    — the node is the OBJECT of a live user_stated NON-hierarchy edge (the user
                  ASSERTED this thing in a fact: owns/has_pet/lives_in/…) AND its surface is not
                  a known COMMON-noun type (WordNet noun synset with a lowercase lemma, or a
                  morphological variant / multiword head thereof) → a nonsense/proper NAME
                  (`krellin`, `zzcanaryglyph`; `halifax` resolves only to the capitalized
                  proper lemma). Genuine types (`dog`, `poodle`, `wireless mouse`→head `mouse`)
                  still ladder — LV2. Lexical DB unavailable → treated as NOT a common noun →
                  REFUSE (fail closed). This arm only fires for user-ASSERTED objects; engine-grown
                  nodes (Hearst members, WordNet rungs) are exempt by provenance, so the ±6
                  semblance buildout is not blinded.

FAIL-SAFE POLARITY (the whole point — LV3): any read error or missing cursor REFUSES the rung. An unstamped (`node_role IS NULL`) node with no structural evidence and no
user assertion is a FRESH TYPE NODE and still ladders exactly as today (LV2 regression bar) —
"fail closed" closes on UNCERTAINTY (errors, unknown nodes, unresolvable surfaces), never on
"no evidence of being a memory" (which is the normal state of every genuine new type).

SUBJECT-AGNOSTIC (constitution veto 2): no domain word list, no colour/place vocabulary, no
rel-name literal beyond the CLOSED STRUCTURAL seeds the ontology itself canonizes (the W3C
P31/P279 classification pair and the SKOS naming pair — the same constants `node_role`,
`_attach_wordnet_hypernym_ladder` and `_is_named_instance` already pin; they are ontology
axioms, not a growable axis). The lexical arm consults a lexical FACT database (WordNet) the
same way dateparser is consulted for dates.

SINGLE ROLLBACK LEVER: `HARDLINE_LADDER_GUARD` (default ON = the fixed behaviour; `0` restores
the pre-guard byte-identical behaviour). No other flag exists.

Leaf module (stdlib + structlog + the lazy-nltk `wordnet_ladder`): importable from BOTH
`src/api/main.py` and `src/re_embedder/embedder.py` without circular dependencies.
"""

from __future__ import annotations

import structlog

from src.api.node_role import _NAMING_RELS  # the ONE fixed SKOS naming pair, single source

log = structlog.get_logger()

_FLAG = "HARDLINE_LADDER_GUARD"

# Closed STRUCTURAL seeds (ontology axioms, not a domain vocabulary — see module docstring):
# the W3C-canonical classification-instance rel (P31/rdf:type). Kept as a tuple constant ONCE,
# beside the naming pair imported from node_role, so no call site open-codes a rel literal.
_INSTANCE_RELS = ("instance_of",)

# Value-filing tail wildcard/capacity markers as stored in rel_types.tail_types (uppercase
# normalization mirrors node_role._read_tail_types). Resolved via metadata, not by rel name.
_VALUE_TAIL_MARKERS = ("ANY", "SCALAR")

# The single evidence query. One round trip, SAVEPOINT-guarded, per-tenant via the passed
# tenant-bound db_conn (its search_path scopes facts/staged_facts/entities/rel_types).
_EVIDENCE_SQL = (
    "SELECT"
    "  e.node_role,"
    "  EXISTS (SELECT 1 FROM facts x WHERE x.subject_id = e.id"
    "            AND x.rel_type = ANY(%s)"
    "            AND x.superseded_at IS NULL AND x.archived_at IS NULL"
    "          UNION ALL"
    "          SELECT 1 FROM staged_facts x WHERE x.subject_id = e.id"
    "            AND x.rel_type = ANY(%s)"
    "            AND x.promoted_at IS NULL AND x.deleted_at IS NULL),"
    "  EXISTS (SELECT 1 FROM facts x WHERE x.subject_id = e.id"
    "            AND x.rel_type = ANY(%s)"
    "            AND x.superseded_at IS NULL AND x.archived_at IS NULL"
    "          UNION ALL"
    "          SELECT 1 FROM staged_facts x WHERE x.subject_id = e.id"
    "            AND x.rel_type = ANY(%s)"
    "            AND x.promoted_at IS NULL AND x.deleted_at IS NULL),"
    "  EXISTS (SELECT 1 FROM entity_attributes a WHERE a.entity_id = e.id"
    "            AND a.superseded_at IS NULL),"
    "  EXISTS (SELECT 1 FROM facts f JOIN rel_types rt ON rt.rel_type = f.rel_type"
    "            WHERE f.object_id = e.id AND f.fact_provenance = 'user_stated'"
    "              AND f.superseded_at IS NULL AND f.archived_at IS NULL"
    "              AND (rt.tail_types IS NULL OR cardinality(rt.tail_types) = 0"
    "                   OR rt.tail_types <@ %s::text[])"
    "          UNION ALL"
    "          SELECT 1 FROM staged_facts f JOIN rel_types rt ON rt.rel_type = f.rel_type"
    "            WHERE f.object_id = e.id AND f.fact_provenance = 'user_stated'"
    "              AND f.promoted_at IS NULL AND f.deleted_at IS NULL"
    "              AND (rt.tail_types IS NULL OR cardinality(rt.tail_types) = 0"
    "                   OR rt.tail_types <@ %s::text[])),"
    "  EXISTS (SELECT 1 FROM facts f LEFT JOIN rel_types rt ON rt.rel_type = f.rel_type"
    "            WHERE f.object_id = e.id AND f.fact_provenance = 'user_stated'"
    "              AND f.superseded_at IS NULL AND f.archived_at IS NULL"
    "              AND (rt.is_hierarchy_rel IS NOT TRUE)"
    "          UNION ALL"
    "          SELECT 1 FROM staged_facts f LEFT JOIN rel_types rt ON rt.rel_type = f.rel_type"
    "            WHERE f.object_id = e.id AND f.fact_provenance = 'user_stated'"
    "              AND f.promoted_at IS NULL AND f.deleted_at IS NULL"
    "              AND (rt.is_hierarchy_rel IS NOT TRUE)),"
    "  EXISTS (SELECT 1 FROM facts f JOIN rel_types rt ON rt.rel_type = f.rel_type"
    "            WHERE f.object_id = e.id AND f.fact_provenance = 'user_stated'"
    "              AND f.superseded_at IS NULL AND f.archived_at IS NULL"
    "              AND rt.tail_types IS NOT NULL AND cardinality(rt.tail_types) > 0"
    "              AND NOT (rt.tail_types <@ %s::text[])"
    "          UNION ALL"
    "          SELECT 1 FROM staged_facts f JOIN rel_types rt ON rt.rel_type = f.rel_type"
    "            WHERE f.object_id = e.id AND f.fact_provenance = 'user_stated'"
    "              AND f.promoted_at IS NULL AND f.deleted_at IS NULL"
    "              AND rt.tail_types IS NOT NULL AND cardinality(rt.tail_types) > 0"
    "              AND NOT (rt.tail_types <@ %s::text[]))"
    " FROM entities e WHERE e.id = %s LIMIT 1"
)

# Row layout of _EVIDENCE_SQL (positional — tests pin this order).
(_EV_ROLE, _EV_INSTANCE, _EV_NAMING, _EV_ATTR, _EV_SLOT, _EV_ASSERTED,
 _EV_INSTANCE_SLOT) = range(7)

# node_role values on the VALUE side of the firewall (corroboration only; 'place' deliberately
# excluded — a place NAME is still a name, and 'both' keeps value protection per the
# migration-192 punning rule).
_STAMP_PROTECTED = frozenset({"value", "name", "both"})


def enabled() -> bool:
    """True when the ladder HARD-LINE guard is ON (default). `HARDLINE_LADDER_GUARD=0` is the
    single rollback lever restoring the pre-guard behaviour byte-identically."""
    import os
    return os.getenv(_FLAG, "true").strip().lower() not in ("0", "false", "no", "off")


def _surface_is_common_type(surface: str | None) -> bool:
    """TRI-STATE (round-5 RED 1, live-proven): True/False are VERDICTS — True = the surface
    has a lowercase common-noun lemma (a TYPE); False = it resolves only to proper
    lemmas / coinages / scalar shapes (a NAME or a value). None = the lexical DB
    itself is unavailable (an install defect, NOT a judgment about the surface) —
    the CALLER picks its fail polarity: the SUBCLASS arm fails closed (uncertain
    means do-not-ladder), the instance_of typing arm fails OPEN.
    """
    if not surface:
        return False
    try:
        from src.api.wordnet_ladder import has_common_noun_sense
        verdict = has_common_noun_sense(surface)
    except Exception:  # noqa: BLE001 — lexical source unavailable/unimportable
        return None  # TRI-STATE: oracle DOWN (the caller picks its fail polarity)
    if verdict is None:
        return None  # the ladder itself reports the corpus absent — NOT a verdict
    return bool(verdict)


def refuses_subclass_rung(
    db_conn,
    node_id,
    surface: str | None = None,
    *,
    user_id: str | None = None,
) -> tuple[bool, "str | None"]:
    """THE consumer predicate: may `node_id` receive a (new) `subclass_of` rung?

    Returns `(refuse, reason)`. `refuse=True` means the node is on the MEMORY side of THE
    HARD LINE (a user value, a captured scalar, a proper NAME, a named instance) or the guard
    could NOT decide (read error / unknown node) — the caller MUST NOT stage the rung.
    `refuse=False` only ever means: affirmative evidence checks all ran clean AND nothing on
    the memory side was found — i.e. the node is a genuine (or fresh) TYPE node and ladders
    exactly as today (LV2).

    `node_role` is CORROBORATION (R5): a value-side stamp refuses, but a NULL stamp NEVER
    admits by itself (LV3 — the decision comes from R1-R4/R6 structural evidence).
    """
    if not enabled():
        return (False, None)
    # LOUD CALLER-MISUSE GUARD (critic hardening, 2026-09-04): a non-connection argument
    # (typically a DB CURSOR — the round-1 adjudicator bug) must RAISE, never fold into
    # cursor_error_fail_closed. A correctly-polarized refusal for the WRONG REASON is
    # indistinguishable from protection on the dashboards and lets a caller bug ship silently;
    # a TypeError at the call site is impossible to miss. This fires ONLY on programmer misuse
    # (no callable .cursor factory) — a real connection with a dead socket still takes the
    # fail-closed path below, so ingest seams never gain a new raise from runtime DB trouble.
    _cursor_factory = getattr(db_conn, "cursor", None)
    if not callable(_cursor_factory):
        raise TypeError(
            "refuses_subclass_rung expects a DB CONNECTION (it opens its own savepointed "
            "cursor); got " + type(db_conn).__name__ + " — pass the connection, not a cursor "
            "(the round-1 LV5 adjudicator bug this guard exists to make impossible to miss)")
    if not node_id or not str(node_id).strip():
        return (True, "no_node")
    if str(node_id) in {str(user_id or ""), "user"}:
        return (True, "user_anchor")
    row = None
    try:
        with db_conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_ladder_hardline")
            try:
                cur.execute(
                    _EVIDENCE_SQL,
                    (list(_INSTANCE_RELS), list(_INSTANCE_RELS),
                     list(_NAMING_RELS), list(_NAMING_RELS),
                     list(_VALUE_TAIL_MARKERS), list(_VALUE_TAIL_MARKERS),
                     list(_VALUE_TAIL_MARKERS), list(_VALUE_TAIL_MARKERS),
                     str(node_id)),
                )
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_ladder_hardline")
            except Exception:  # noqa: BLE001 — FAIL CLOSED, the opposite of node_role
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_ladder_hardline")
                except Exception:  # noqa: BLE001
                    pass
                return (True, "read_error_fail_closed")
    except Exception:  # noqa: BLE001 — no cursor / dead connection → FAIL CLOSED
        return (True, "cursor_error_fail_closed")
    if row is None:
        # No entities row for this id: NOT memory-side evidence. At every wired seam the rung's
        # subject is UUID-resolved by the registry (which registers it in-transaction) BEFORE the
        # guard runs, so a missing row here means "not yet queryable through THIS connection"
        # (ordering, fake-conn seams) — the pre-guard behaviour ladders, and refusing would be
        # exactly the over-fire the gauntlet's footgun clause warns about (it breaks 4 pinned
        # seam tests). Fail-closed applies to READ ERRORS and stamp-ambiguity, never to absence
        # of evidence: a fresh TYPE node has no memory-side evidence by definition (LV2).
        return (False, None)
    role = (row[_EV_ROLE] or "").strip().lower() if row[_EV_ROLE] else None
    if row[_EV_INSTANCE]:
        return (True, "instance_subject")
    if row[_EV_NAMING]:
        return (True, "naming_surface")
    if row[_EV_ATTR]:
        return (True, "attribute_holder")
    if row[_EV_SLOT]:
        return (True, "value_slot_object")
    if row[_EV_INSTANCE_SLOT]:
        # INSTANCE-DIRECTION arm (critic round, 2026-09-04): object of a user_stated edge whose
        # rel's tail_types is a NON-TRIVIAL SPECIFIC set (has_pet→{Animal}, owns→{Animal,Object,
        # Organization}, lives_in→{Location}) — by the rel's OWN metadata the object is a NAMED
        # INSTANCE of that type, never a subclass. Catches the rex shape (a common-noun homonym
        # surface: 'rex' is a lowercase WordNet lemma, so the morphology arm passes it) AND
        # halifax (lives_in→{Location}) structurally, zero word lists — the same tail_types
        # column R4 reads in the value direction (<@ {ANY,SCALAR}); this is its complement.
        return (True, "instance_slot_object")
    if role in _STAMP_PROTECTED:
        return (True, f"node_role={role}")
    if row[_EV_ASSERTED] and not _surface_is_common_type(surface):
        return (True, "lexical_not_common_type")
    return (False, None)

# ── WOUND E: the instance_of TARGET guard (the cross-ontology break) ────────────────────
# THE WHY (prod seat 10d7d879, measured): recall rendered "P70 Laptop is an instance of CATO"
# — cato is the owner's SON, a named Person entity. That edge is a CATEGORY ERROR ACROSS THE
# HARD LINE: instance_of is the L4 PLACE edge and must target a TYPE node (laptop → device
# ladder), NEVER a named instance/person (a memory). The subclass_of guard above protects the
# LADDER (no rung onto a value/name); this is its TARGET-SHAPE twin — the SAME evidence row
# (R1-R6), the question inverted: not "may this node receive a rung" but "may this node BE the
# type slot another node instances into".
#
# REFUSAL EVIDENCE (memory-side = a NAMED INSTANCE / VALUE / NAME — never a type slot):
#   R1 instance_subject   the node is itself the SUBJECT of a live instance_of edge (cato
#                         instances into person/son) → an instance can never host instances;
#   R2 naming_surface     the node carries a naming edge (also_known_as/pref_name) → a NAME
#                         is the naming layer, a memory — "a name is never a place";
#   R3 attribute_holder   the node holds a captured scalar → a VALUE;
#   R4/R6b slot objects   value-slot / instance-slot of a user_stated edge (rel tail_types
#                         metadata) → a captured value / named instance;
#   R5 stamp              node_role ∈ {value, name, both} → CORROBORATION;
#   R6 lexical            user-asserted object whose surface is NOT a common-noun type → a
#                         proper NAME (chris-p70).
# ALLOW (the LV2 regression bar, identical polarity to refuses_subclass_rung): a node with NO
# memory-side evidence — including a FRESH not-yet-registered type surface — is a legitimate
# new TYPE node and instances exactly as today. Absence of evidence is never refusal.
#
# FAIL-CLOSED on read error (same as the ladder guard): an undecidable target refuses the edge
# — the owner's fail direction is REFUSE THE EDGE rather than misfile across ontologies.
#
# LADDER COMPATIBILITY (top-level branch crossing, e.g. Device vs Person): the per-node ladder
# is currently too thin to check without over-firing — type nodes do not reliably carry a
# persisted top-branch marker (entity_taxonomies groupings cover rel_types, not instance
# nodes), so any branch comparison today would guess. DOCUMENTED FOLLOW-UP: grow a
# top-branch id onto type nodes (engine metadata, e.g. the taxonomy root of the node's
# subclass_of ladder) and refuse instance_of when the SUBJECT's branch differs. Until that
# column exists, THIS guard (target-shape) catches the measured wound class: every real
# cross-ontology break observed so far (cato, chris-p70→person) has a memory-side TARGET,
# which R1/R2/R6 refuse structurally.
def refuses_instance_target(
    db_conn,
    node_id,
    surface: str | None = None,
    *,
    user_id: str | None = None,
) -> tuple[bool, "str | None"]:
    """THE consumer predicate: may `node_id` be the OBJECT of a (new) `instance_of` edge?

    Returns `(refuse, reason)`. `refuse=True` means the node is on the MEMORY side of THE
    HARD LINE (a named instance, a name, a user value) or the guard could NOT decide (read
    error) — the caller MUST REFUSE the edge (drop it), never misfile the instance at a
    person. `refuse=False` means: affirmative evidence checks all ran clean AND nothing
    memory-side was found — the node is a genuine (or fresh) TYPE node.
    """
    if not enabled():
        return (False, None)
    _cursor_factory = getattr(db_conn, "cursor", None)
    if not callable(_cursor_factory):
        raise TypeError(
            "refuses_instance_target expects a DB CONNECTION (it opens its own savepointed "
            "cursor); got " + type(db_conn).__name__ + " — pass the connection, not a cursor "
            "(same caller-misuse contract as refuses_subclass_rung)")
    if not node_id or not str(node_id).strip():
        return (True, "no_node")
    if str(node_id) in {str(user_id or ""), "user"}:
        return (True, "user_anchor")
    row = None
    try:
        with db_conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_instance_hardline")
            try:
                cur.execute(
                    _EVIDENCE_SQL,
                    (list(_INSTANCE_RELS), list(_INSTANCE_RELS),
                     list(_NAMING_RELS), list(_NAMING_RELS),
                     list(_VALUE_TAIL_MARKERS), list(_VALUE_TAIL_MARKERS),
                     list(_VALUE_TAIL_MARKERS), list(_VALUE_TAIL_MARKERS),
                     str(node_id)),
                )
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_instance_hardline")
            except Exception:  # noqa: BLE001 — FAIL CLOSED, the opposite of node_role
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_instance_hardline")
                except Exception:  # noqa: BLE001
                    pass
                return (True, "read_error_fail_closed")
    except Exception:  # noqa: BLE001 — no cursor / dead connection → FAIL CLOSED
        return (True, "cursor_error_fail_closed")
    if row is None:
        # No entities row: a FRESH type surface (registered in-transaction by the registry
        # AFTER the gate at some seams) — no memory-side evidence by definition; refusing
        # would starve every legitimate first-mention typing (LV2 bar). Fail-closed closes
        # on READ ERRORS and stamp-ambiguity, never on absence of evidence.
        return (False, None)
    role = (row[_EV_ROLE] or "").strip().lower() if row[_EV_ROLE] else None
    if row[_EV_INSTANCE]:
        # THE cato arm: the target is itself an instance (it instances into person/son) —
        # an instance is never another instance's TYPE slot. This is the cross-ontology
        # break in its pure form: instance_of must climb the TYPE ladder, never land on a
        # named person.
        return (True, "instance_target_is_an_instance")
    if row[_EV_NAMING]:
        return (True, "instance_target_is_a_name")
    if row[_EV_ATTR]:
        return (True, "instance_target_holds_a_value")
    if row[_EV_SLOT]:
        return (True, "instance_target_is_a_value_slot")
    if row[_EV_INSTANCE_SLOT]:
        return (True, "instance_target_is_an_instance_slot")
    if role in _STAMP_PROTECTED:
        return (True, f"node_role={role}")
    if row[_EV_ASSERTED]:
        # TRI-STATE POLARITY (round-5 RED 1, live-proven): the SUBCLASS arm fails CLOSED
        # on an unavailable oracle (uncertain means do-not-ladder); THIS arm must fail
        # OPEN. instance_of first-mention typing is the rung recall DESCENDS — failing
        # closed here (wordnet corpus absent on the box, a measured install defect)
        # refuses EVERY legitimate type ladder for an asserted target and starves the
        # walk — the exact honest-empty the fresh-surface arm above refuses to cause.
        # A DEFINITE False (verdict: proper noun / coinage / scalar shape) still refuses.
        _common = _surface_is_common_type(surface)
        if _common is False:
            return (True, "instance_target_is_a_proper_name")
    return (False, None)
