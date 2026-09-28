"""ARRIVAL WELD GUARD — refuse an alias write that asserts the WRONG referential identity.

See the internal design record for the cross-tenant measurement behind every line here.

TERMINOLOGY: "seat" below means the user a per-user schema (``faultline_<uuid>``) belongs to —
the tenant owner. Identifiers such as ``seat_entity_id`` / ``is_seat_anchor`` keep that name.

WHAT A "WELD" IS
----------------
"Weld" is a LOCAL COINAGE for this codebase, not a term of art. The established names for the
error it guards are **over-merge**, **entity conflation**, and **false-positive match**; for
over-asserted co-reference specifically, see Halpin, Hayes, McCusker, McGuinness & Thompson,
"When owl:sameAs Isn't the Same: An Analysis of Identity in Linked Data" (ISWC 2010), which
measures exactly this failure — identity links asserted far more strongly than the data warrants.

A WELD := adding an ADDITIONAL, DIFFERENT surface to an entity that already carries labels.

Why that is a truth claim and not indexing: ``entity_aliases`` is modelled on SKOS lexical
labels. W3C SKOS Reference §5.4 states integrity conditions **S13** ("skos:prefLabel,
skos:altLabel and skos:hiddenLabel are pairwise disjoint properties") and **S14** ("A resource
has no more than one value of skos:prefLabel per language tag") — both scoped to *a resource*.
The relevant point is STRUCTURAL: in that data model every label hangs off ONE resource, so
attaching two surfaces to one entity row-set is, **in FaultLine's own model**, the assertion
that they label one entity.

Read the limits honestly, because they bound what may be claimed here: SKOS §1.3 says label
assertions are "facts about the thesaurus… not facts about the way the world is arranged";
§3.5.1 makes no statement relating ``skos:Concept`` to ``owl:Class``; and §5.6.1 states that
"no domain is stated" for the label properties. So SKOS supplies the *data model* this guard
reasons over. It does NOT entail anything about the world, and no such entailment is used.

THE HARD LINE, AND WHAT RDFS DOES AND DOES NOT LICENSE
-----------------------------------------------------
FaultLine's founding split is MEMORY (what the user told you — a name, a value, an instance) vs
PLACE (the L4 type/class hierarchy the engine builds).

RDF Schema 1.1 §3.3: "A triple of the form: ``R rdf:type C`` states that C is an instance of
``rdfs:Class`` and R is an instance of C."  §3.4: "A triple of the form: ``C1 rdfs:subClassOf
C2`` states that C1 is an instance of ``rdfs:Class``, C2 is an instance of ``rdfs:Class`` and C1
is a subclass of C2."

So the OBJECT of an ``rdf:type``-role edge and BOTH ends of an ``rdfs:subClassOf``-role edge are
POSITIVE evidence that the node is being used as a class.

⚠️ WHAT IS **NOT** ENTAILED, and an earlier version of this module wrongly asserted: that a node
appearing only as the SUBJECT of ``rdf:type`` is therefore an individual. RDF Schema §2 says "A
class may be a member of its own class extension and may be an instance of itself" (§2, NOT §3
— an earlier version of this file cited the wrong section; re-verified against the spec), and
``rdf:type`` has ``rdfs:domain rdfs:Resource`` — subject position carries ZERO class-hood
information. Class/individual disjointness is an OWL DL constraint, not an RDFS one.

This guard therefore relies ONLY on the positive direction: class-side position is evidence a
node is USED as a class in this tenant's graph. Absence of that evidence is absence of evidence,
never proof of individual-hood — which is exactly why the guard's default on no evidence is
ALLOW.

THE EVIDENCE MUST ITSELF SATISFY THE AUTHORITY ORDER (the fix that made this safe)
---------------------------------------------------------------------------------
Authority order is user > seed > growth, and it applies to the EVIDENCE, not only to the label.
An earlier version admitted ANY class-side edge, so an ``llm_inferred`` Class-B edge minted by
engine growth could VETO a ``user_stated`` label — growth overriding user truth, constraint 12
exactly backwards. Measured on production, that mis-refused the seat owner's own given name on
their own user entity, on 2 of 12 tenants.

Class-side evidence is now admissible ONLY when the edge's own provenance is user-stated: the
user themselves said this node is a kind/class of something. Measured effect across all 12
tenants: entities where a new user-stated label would be refused drops 1486 → 194, and
seat-owner false refusals drop 2 → 0, while every genuine type-node collapse stays refused.

THE SEAT ANCHOR IS NEVER REFUSED
--------------------------------
The seat owner's entity is the resolution target of the engine's single language hook
("I / me = the user"). Refusing a label there does NOT merely cost reachability — ``resolve()``
would mint ``uuid5(user_id, <name>)``, which is NOT the user entity, so the user's own name
detaches from the speaker and grounds onto a stranger. It is exempted unconditionally, detected
STRUCTURALLY (never by a name or pronoun list) by two independent markers, unioned:
  1. the UUID-shaped suffix of the bound schema name (measured: derives the seat 12/12), and
  2. an alias whose ``preference_source`` is the provisioning tier (measured: present 11/12, and
     exactly one entity per tenant carries it — never a non-seat entity).
Neither marker alone is sufficient: marker 2 is absent on one tenant, and marker 1 needs a bound
schema name. Together they covered 12/12.

AUTHORITY BANDS (constraint 12), read from RECORDED provenance
--------------------------------------------------------------
Bands are derived by RANK from the existing ladder ``registry._PREFERENCE_RANK``:
  * MEMORY authority     := ``rank(src) >= rank("user_stated")``
  * ENGINE-GROWTH claim  := ``rank("provisioned") < rank(src) < rank("user_stated")``
  * at/below the provisioning floor := NO CLAIM RECORDED — notably the origin label minted by
    ``EntityRegistry.resolve()``, which takes no source parameter and lands at the column
    default. Explicitly NOT a growth claim; this distinction is what yields zero false refusals.

THE WARRANT AXIS — ORTHOGONAL TO RANK, AND WHY ARM 3 NEEDED IT
--------------------------------------------------------------
Rank answers "how far do I trust this source to pick the DISPLAY name?". It does not answer
"was this co-reference claim LICENSED by anything?", and those are different questions. While
they were collapsed into one number, the third arm was inexpressible: the WordNet
canonicalization lane and an extraction-invented weld both arrived as ``inferred`` and were
byte-identical at the write, so no rule over the alias table could separate a warranted
co-reference from an unwarranted one.

``registry._WARRANTED_SOURCES`` records the licence separately. The lexical lane
(``src/api/canonicalize.py``) registers a node's WordNet co-synset lemmas, and shared synset
membership is what synonymy MEANS in that resource — Princeton WordNet groups "nouns, verbs,
adjectives and adverbs … into sets of cognitive synonyms (synsets), each expressing a distinct
concept" (WordNet homepage; canonical citation G. A. Miller, "WordNet: A Lexical Database for
English", Communications of the ACM 38(11):39-41, 1995). So that lane can NAME the authority
licensing its claim, and an extraction guess cannot.
  ⚠️ HONESTY NOTE ON THAT CITATION: the Miller 1995 reference was verified. The homepage
  sentence is quoted as attested by several independent mirrors — wordnet.princeton.edu returns
  HTTP 403 to automated fetch, so it was NOT read directly from the primary host here.

The warrant buys the lane NO EXTRA TRUST: ``lexical`` ranks EQUAL to ``inferred``. It buys only
the right to assert co-reference. Anything not listed is unwarranted, which is the safe default
— no claim recorded means no licence.

WHY REFUSING IS THE SAFE DIRECTION (where it is safe at all)
------------------------------------------------------------
Record linkage has been a decision problem with an explicit error trade-off since Fellegi &
Sunter, "A Theory for Record Linkage" (JASA 64(328):1183-1210, 1969), which formalises linkage
as choosing between link / non-link / possible-link under bounded error rates. Here the two
errors are asymmetric *for non-seat entities*: a refused weld costs REACHABILITY (the surface
still resolves to its own entity via the UUID-v5 surrogate — no user content is deleted), while
an accepted wrong weld costs TRUTH irreversibly, since no column records which referent a
surface arrived with. For the SEAT entity the asymmetry reverses, which is why it is exempt.

Subject-agnostic by construction: no entity name, no type name, no rel name, no pronoun, no
domain word, no root. Every decision is (graph shape) x (recorded provenance rank).

FAIL-SAFE / FAIL-LOUD
---------------------
Every probe runs in its own SAVEPOINT and fails OPEN. A probe that errors must never abort the
caller's transaction — an aborted transaction upstream is how a previous defect on this exact
table rendered "no facts found" over a memory the walk had already read. Refusals log CRITICAL.
"""
from __future__ import annotations

import os
import re
import uuid

import structlog

try:  # pragma: no cover - logging_config is always present in-app
    from src.api.logging_config import log_crit
except Exception:  # noqa: BLE001 — never let an import shape the write path
    def log_crit(logger, msg, **kwargs):  # type: ignore[misc]
        logger.critical(msg, **kwargs)

log = structlog.get_logger()

# RDFS role handles, resolved through rel_types.wikidata_pid (the rel NAMES stay DB-grown).
# P31 = "instance of" (rdf:type role, RDF Schema §3.3); P279 = "subclass of"
# (rdfs:subClassOf role, RDF Schema §3.4).
_PID_INSTANCE_OF = "P31"
_PID_SUBCLASS_OF = "P279"

# A UUID-shaped suffix on the bound schema name yields the seat owner's entity id. Prefix-
# agnostic on purpose (it must hold for any tenant schema family), and it is a SHAPE, not
# a name — no schema prefix literal is matched.
_SCHEMA_SEAT_SUFFIX = re.compile(
    r"([0-9a-fA-F]{8})_([0-9a-fA-F]{4})_([0-9a-fA-F]{4})_([0-9a-fA-F]{4})_([0-9a-fA-F]{12})$"
)

_ENV_MODE = "ALIAS_WELD_GUARD"
# ENFORCE IS THE DEFAULT (was "observe"), and what enforces by default is ARM 1 ONLY.
# ARMs 2 and 3 are built and measured but OFF behind their own switches; neither met the bar.
#
# Replayed READ-ONLY over the complete weld history of all 12 production tenants against a
# replica verified equal to prod (row counts, is_preferred counts, live-fact counts and
# wikidata_pid counts) before measuring:
#
#   ⚠️ STATE THE ELIGIBLE DENOMINATOR, NOT THE RAW ONE. ARM 1 can only fire on a weld whose
#   incoming label is at/above memory authority AND not explicitly preferred. Of 313 raw
#   historical welds, 291 were STRUCTURALLY UN-REFUSABLE and only 22 were eligible — 18 of
#   them on one tenant. A "313 welds / 12 tenants / zero false refusals" headline reads as
#   broad validation of a 22-row evidence base; an earlier revision of this comment did
#   exactly that. The eligible count is the number that matters.
#
#   ELIGIBLE welds 22 → 6 refusals, every one a NAME welded onto an entity the USER declared
#   a class (a pet's name welded onto {dog}, {cat}, a breed node, a species node — four
#   distinct class nodes, two pet names). Every refusal verified class-side with USER-STATED evidence.
#   FORWARD blast radius, TOTAL across armed arms: 194 entities. Sampled: common-noun type
#   nodes (runtime/driver/process/servidor) — where a user's name must never land.
#   SEAT-OWNER false refusals: 0 historical, 0 forward-looking.
#   Legitimate multi-alias entities: 0 refused — a spouse known by name + nickname + role,
#   children known by name + role, a given name + its short form, and the tenant owner's own
#   {user, <given name>, <full given name>} all pass.
#   Lexical canonicalization lane: 0 refusals, proven by a positive control, not by absence.
_DEFAULT_MODE = "enforce"

# ARM 3 has its OWN switch and it is OFF by default, deliberately — read the measurement
# before changing it (the internal design record §7).
#
# ARMs 1-2 measured ZERO false refusals across all 12 production tenants, which is why the
# module default is `enforce`.
#
# ── ARM 3, RE-MEASURED after the producer-side warrant landed (2026-08-11) ─────────────────
# The blocker is CLOSED. The one legitimate refusal in 48 was `{sustainable aviation fuel} ≡
# {saf}` — an acronym the user had DEFINED in their own sentence ("…using sustainable aviation
# fuel (SAF)", read verbatim out of that tenant's `episodic_log`). It was never an arm defect:
# the generic extraction `also_known_as` lane emitted that definition and an invented
# co-reference with identical provenance and no recorded licence, and no rule downstream can
# separate what the producer conflated. `src/extraction/abbreviation.py` now lets the producer
# name its licence (Schwartz & Hearst 2003, training-free, no word list), and the weld is
# ADMITTED by its recorded warrant.
#
#   Re-replayed READ-ONLY over all 12 production tenants against a replica verified equal to
#   prod (alias / is_preferred / live-fact / staged / rel_type-pid / entity / episodic counts
#   all identical):
#     ELIGIBLE welds for ARM 3 (incoming = UNWARRANTED engine-growth claim): 276 of 313.
#     Refusals 48 -> 47. `{sustainable aviation fuel} ≡ {saf}` ADMITTED. NO new refusals.
#     Warrants granted across the entire weld history: EXACTLY 1 (the SAF pair).
#     With ARM 3 off, the warrant is a NO-OP: refusal set byte-identical to the pre-warrant run.
#     Seat-owner false refusals: 0 historical, 0 forward.
#     Corpus leak survey — the matcher licenses 12 distinct pairs across 5 443 real turns;
#     9 are genuine definitions ({cpa}/{certified public accountant}, {nat}/{network address
#     translation}, a nickname defined for a person's name, …), 3 are spurious ({free}/{freemium does not count}) and
#     NONE of the 3 coincides with any weld, so their measured production effect is zero.
#
# IT IS STILL `off`, AND THAT IS DELIBERATE — the remaining reason is no longer the warrant.
# Two things the bar demands are not yet in hand:
#   (1) Each of the 47 survivors was read against the turn that produced it. None is an
#       identity the USER asserted. But 3 are class-vs-class or class-vs-subclass merges
#       ({photo opportunities} ≡ {instagram-worthy shots}, {wildlife} ≡ {bears}, {gospel} ≡
#       {bible books}) whose legitimacy is a PRODUCT JUDGEMENT, not a measurement — and a
#       judgement call cannot clear a bar of zero.
#   (2) ARM 1 was armed only after an OBSERVE run on production measured its real rate. ARM 3
#       has had no such run, and its forward blast radius is 506 entities — 8.6% of every
#       entity in production, and unlike ARM 1's 194 it declines the COMMON case (an engine
#       label) rather than the rare one. `ALIAS_WELD_GUARD_ARM3=observe` now exists precisely
#       so that run can happen WITHOUT standing ARM 1 down (see `arm3_mode`). That is the next
#       step, and it is the owner's call, not this module's default.
_ENV_ARM3 = "ALIAS_WELD_GUARD_ARM3"
_DEFAULT_ARM3 = "off"

# ARM 2 is also OFF by default — see `arm2_enabled` for the measurement. Short version: it
# shipped without the place condition ARM 3 was given, which made it refuse a new user-stated
# label on 2 526 entities, 2 133 of them not classes at all; and the authority-safe place
# condition renders it a strict subset of ARM 1, i.e. provably subsumed.
_ENV_ARM2 = "ALIAS_WELD_GUARD_ARM2"
_DEFAULT_ARM2 = "off"

ALLOW = "allow"
ARM_PLACE = "place_takes_no_memory_label"
ARM_GROWTH_ONLY = "memory_label_onto_growth_only_node"
ARM_UNWARRANTED = "unwarranted_growth_coreference"


def guard_mode() -> str:
    """Resolve the guard mode from the environment on every call (test/ops friendly)."""
    mode = (os.environ.get(_ENV_MODE) or _DEFAULT_MODE).strip().lower()
    return mode if mode in ("off", "observe", "enforce") else _DEFAULT_MODE


def arm2_enabled() -> bool:
    """Is ARM 2 armed? OFF by default — it is PROVABLY SUBSUMED once made authority-safe.

    ARM 2 as originally written had NO place condition, and review caught that: the argument
    for why the place condition is load-bearing was written for ARM 3, measured (dropping it
    costs 49 legitimate refusals), applied to ARM 3 — and ARM 2 never got it. Measured on
    production, ARM 2 would refuse a new user-stated label on 2 526 entities, of which 2 133
    are NOT class-side by ANY provenance. Those are INSTANCE nodes, and this module's own §7
    declares adding a surface to an instance to be legitimate indexing. Concrete refusals
    included `{transportation management}+{tms}` and `{configuration}+{config}` — verbatim the
    examples cited as LEGITIMATE when justifying ARM 3's place condition.

    Adding the condition is not optional and it cannot use engine evidence: ARM 2 declines a
    USER-STATED label, so admitting growth-authored placement would invert the authority order
    — the exact regression that made revision 1 refuse the seat owner's own given name.

    But with `require_user_evidence=True`, ARM 2 becomes **unreachable**. ARM 1 refuses on
    `_entity_is_user_asserted_place` ALONE and runs FIRST, so ARM 2's condition set is a strict
    subset of ARM 1's. Measured marginal value: **zero refusals, zero forward entities**.

    It is therefore OFF, and kept behind a switch rather than deleted so the measurement stays
    reproducible and the arm can be revisited if it is ever given a condition that is both
    authority-safe and not subsumed. `ALIAS_WELD_GUARD_ARM2=on` arms it (still place-gated).
    """
    val = (os.environ.get(_ENV_ARM2) or _DEFAULT_ARM2).strip().lower()
    return val in ("1", "true", "on", "yes", "enforce", "enabled")


def arm3_mode() -> str:
    """``off`` | ``observe`` | ``enforce`` — ARM 3's OWN mode, independent of the module's.

    WHY THIS EXISTS, AND IT IS A GAP THE ARM-1 ROLLOUT DID NOT HAVE TO CROSS. ARM 1 was armed
    only after a production run in ``observe`` measured its real refusal rate. That path is not
    available to ARM 3 as the module was written: ``ALIAS_WELD_GUARD=observe`` downgrades the
    WHOLE guard, so measuring ARM 3 on production would mean standing ARM 1 down at the same
    time — trading a shipped, enforcing protection for a measurement.

    ``ALIAS_WELD_GUARD_ARM3=observe`` closes that: ARM 3 evaluates in full and logs every weld
    it WOULD refuse, returns ALLOW, and leaves ARM 1 enforcing untouched. It is the same
    evidence ladder ARM 1 climbed, and it is what the arm needs before its default can move.

    Unrecognised values resolve to ``off`` — the fail-safe direction is always "do not refuse".
    """
    val = (os.environ.get(_ENV_ARM3) or _DEFAULT_ARM3).strip().lower()
    if val in ("1", "true", "on", "yes", "enforce", "enabled"):
        return "enforce"
    if val in ("observe", "log", "measure"):
        return "observe"
    return "off"


def arm3_enabled() -> bool:
    """Is ARM 3 ENFORCING? ``observe`` is deliberately NOT enabled — it never refuses."""
    return arm3_mode() == "enforce"


def _rank(source):
    """Authority rank via the EXISTING ladder — imported lazily to avoid a circular import."""
    from src.entity_registry.registry import preference_rank
    return preference_rank(source)


def _warranted(source):
    """Did this provenance arrive with a RECORDED WARRANT? (registry._WARRANTED_SOURCES)"""
    from src.entity_registry.registry import preference_is_warranted
    return preference_is_warranted(source)


def _memory_tier():
    return _rank("user_stated")


def _is_growth_claim(source):
    """A claim AUTHORED BY ENGINE GROWTH: strictly above the provisioning floor, below memory.

    At/below the floor is NOT a growth claim — it is NO CLAIM RECORDED (notably the origin
    label ``EntityRegistry.resolve()`` mints, which takes no source parameter and lands at the
    column default). That floor/claim distinction is load-bearing: it is what keeps every
    legitimately-named entity out of ARM 2 and ARM 3.
    """
    return _rank("provisioned") < _rank(source) < _memory_tier()


def is_growth_claim(source) -> bool:
    """PUBLIC: was this alias provenance AUTHORED BY ENGINE GROWTH?

    Exposed for the PRODUCERS, not just this guard. A producer that can NAME the authority
    licensing its co-reference claim (see ``registry._WARRANTED_SOURCES``, and
    ``src/extraction/abbreviation.py`` for the abbreviation-definition lane) must rewrite ONLY
    an unwarranted GROWTH claim into a warranted one — never a user-stated source (that would
    demote user truth beneath a growth claim, the authority order backwards) and never a source
    at or below the provisioning floor (which records no claim to license in the first place).

    Sharing this predicate with the guard is the point: the arm that READS the warrant and the
    lane that WRITES it must agree on which band is rewritable, and two copies of that band
    definition are how they drift apart.
    """
    return _is_growth_claim(source)


def seat_entity_id(schema_name):
    """Derive the seat owner's entity id from the bound schema name, or None.

    Structural: the schema name ends in a UUID-shaped, underscore-separated slug and the seat
    owner's entity id IS that UUID. Measured to derive the correct seat on 12/12 tenants.
    """
    if not schema_name:
        return None
    m = _SCHEMA_SEAT_SUFFIX.search(str(schema_name))
    if not m:
        return None
    try:
        return str(uuid.UUID("-".join(m.groups()))).lower()
    except (ValueError, AttributeError):
        return None


def _savepoint(cur, name):
    try:
        cur.execute(f"SAVEPOINT {name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query name is a savepoint identifier (alphanumeric, controlled by caller), not user data
        return name
    except Exception:  # noqa: BLE001 — autocommit / no transaction: run without one
        return None


def _release(cur, name):
    if name:
        try:
            cur.execute(f"RELEASE SAVEPOINT {name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query name is a savepoint identifier, not user data
        except Exception:  # noqa: BLE001
            pass


def _rollback(cur, name):
    if name:
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query name is a savepoint identifier, not user data
        except Exception:  # noqa: BLE001
            pass


def _entity_is_place(cur, entity_id, require_user_evidence=True) -> bool:
    """True when this entity occupies a CLASS-SIDE position in the tenant's graph.

    Class-side position (RDF Schema §3.3/§3.4): OBJECT of an ``rdf:type``-role edge, or SUBJECT
    of an ``rdfs:subClassOf``-role edge. Rel identity comes from ``rel_types.wikidata_pid``,
    which is DB-grown per tenant — no rel name is hardcoded.

    ``require_user_evidence`` selects WHOSE placement counts, and the two callers need
    genuinely different answers — this is an authority question, not a tuning knob:

    * **True (ARM 1, the default).** THE EVIDENCE ITSELF MUST CARRY USER AUTHORITY, because
      ARM 1 declines a ``user_stated`` label. An engine-grown placement edge must never be able
      to veto user truth (constraint 12: growth may add and widen, never override). Dropping
      this filter is exactly the revision-1 regression that refused the seat owner's own given
      name on 2 of 12 production tenants.
    * **False (ARM 3).** ARM 3 declines an ENGINE claim, so there is no higher authority in the
      weld for engine evidence to invert. Growth checking growth against growth is inside the
      authority order, not against it, so ANY class-side placement is admissible evidence
      there. Requiring user evidence in ARM 3 would not be conservative, it would simply blind
      the arm on precisely the engine-built type nodes it exists to protect.

    Durable rows are filtered on ``facts.fact_provenance``, staged rows on
    ``staged_facts.provenance``, both against the top tier of the authority ladder.

    SAVEPOINT-guarded; any failure returns False (allow).
    """
    sp = _savepoint(cur, "sp_weld_place")
    tier = "user_stated"
    # ONE statement for both callers. The provenance predicate is the ONLY difference, and it
    # is a fixed fragment chosen from this module (never caller data), so no SQL is built from
    # anything untrusted. Keeping it as one statement is deliberate: two copies of a four-way
    # UNION over the same tables is exactly how the subject/object direction on the P31 arm
    # would get silently inverted in one copy and not the other.
    prov_fact = "f.fact_provenance = %s" if require_user_evidence else "f.fact_provenance IS NOT NULL"
    prov_stag = "s.provenance = %s" if require_user_evidence else "s.provenance IS NOT NULL"
    args = ([entity_id, _PID_INSTANCE_OF] + ([tier] if require_user_evidence else [])
            + [entity_id, _PID_SUBCLASS_OF] + ([tier] if require_user_evidence else [])
            + [entity_id, _PID_INSTANCE_OF] + ([tier] if require_user_evidence else [])
            + [entity_id, _PID_SUBCLASS_OF] + ([tier] if require_user_evidence else []))
    try:
        cur.execute(
            "SELECT 1 FROM facts f JOIN rel_types r ON r.rel_type = f.rel_type"
            "  WHERE f.object_id = %s AND r.wikidata_pid = %s"
            f"    AND {prov_fact}"
            "    AND f.superseded_at IS NULL AND f.archived_at IS NULL"
            " UNION ALL "
            "SELECT 1 FROM facts f JOIN rel_types r ON r.rel_type = f.rel_type"
            "  WHERE f.subject_id = %s AND r.wikidata_pid = %s"
            f"    AND {prov_fact}"
            "    AND f.superseded_at IS NULL AND f.archived_at IS NULL"
            " UNION ALL "
            "SELECT 1 FROM staged_facts s JOIN rel_types r ON r.rel_type = s.rel_type"
            "  WHERE s.object_id = %s AND r.wikidata_pid = %s"
            f"    AND {prov_stag}"
            "    AND s.promoted_at IS NULL AND s.deleted_at IS NULL"
            " UNION ALL "
            "SELECT 1 FROM staged_facts s JOIN rel_types r ON r.rel_type = s.rel_type"
            "  WHERE s.subject_id = %s AND r.wikidata_pid = %s"
            f"    AND {prov_stag}"
            "    AND s.promoted_at IS NULL AND s.deleted_at IS NULL"
            " LIMIT 1",
            tuple(args),
        )
        found = cur.fetchone() is not None
        _release(cur, sp)
        return found
    except Exception as err:  # noqa: BLE001 — fail OPEN, never poison the caller's transaction
        _rollback(cur, sp)
        log.warning("weld_guard.place_probe_failed",
                    entity=str(entity_id)[:16], error=str(err)[:200],
                    require_user_evidence=require_user_evidence,
                    note="failing OPEN — the weld is allowed; the guard never blocks on its own error")
        return False


def _entity_is_user_asserted_place(cur, entity_id) -> bool:
    """ARM 1's predicate: class-side AND the placement evidence itself is user-stated.

    Kept as a named function because that conjunction — not "class-side" alone — is the thing
    revision 2 had to fix, and a reader (or a mutation test) should be able to point at it.
    """
    return _entity_is_place(cur, entity_id, require_user_evidence=True)


def _existing_labels(cur, entity_id):
    """Return ``[(alias, preference_source), …]`` already held by ``entity_id``.

    Returns ``None`` (distinct from ``[]``) when the probe FAILED, so the caller fails open
    instead of mistaking an error for "brand new entity".
    """
    sp = _savepoint(cur, "sp_weld_labels")
    try:
        cur.execute(
            "SELECT alias, preference_source FROM entity_aliases WHERE entity_id = %s",
            (entity_id,),
        )
        rows = [(r[0], r[1]) for r in (cur.fetchall() or [])]
        _release(cur, sp)
        return rows
    except Exception as err:  # noqa: BLE001
        _rollback(cur, sp)
        log.warning("weld_guard.label_probe_failed",
                    entity=str(entity_id)[:16], error=str(err)[:200],
                    note="failing OPEN — the weld is allowed")
        return None


def _is_seat_anchor(entity_id, existing, schema_name) -> bool:
    """Structural seat-owner detection — the union of two independent markers.

    Marker 1: the entity id equals the UUID-shaped suffix of the bound schema name.
    Marker 2: the entity carries a label written at the PROVISIONING tier of the authority
              ladder — a marker only the provisioning path writes.

    Measured across 12 production tenants: marker 1 alone = 12/12, marker 2 alone = 11/12 (and
    never on a non-seat entity). Neither is dropped: marker 1 needs a bound schema name, which
    not every caller supplies.
    """
    seat = seat_entity_id(schema_name)
    if seat and str(entity_id).strip().lower() == seat:
        return True
    prov_tier = _rank("provisioned")
    return any(_rank(src) == prov_tier for _, src in (existing or []))


def is_seat_anchor(cur, entity_id, schema_name=None) -> bool:
    """PUBLIC: is ``entity_id`` the SEAT OWNER's entity? Structural — no name/pronoun list.

    Same two-marker union `weld_verdict` uses internally (see ``_is_seat_anchor`` and the
    module docstring), exposed so other call sites answer "is this the first-person anchor?"
    the SAME way instead of re-deriving it from a token list. Measured on 12 production
    tenants: marker 1 (UUID-shaped suffix of the bound schema name) 12/12, marker 2 (an alias
    at the provisioning tier) 11/12 — and marker 2 lands on exactly one entity per tenant,
    never on a non-seat entity.

    ⚠️ THE FAIL DIRECTION IS THE OPPOSITE OF ``weld_verdict``'S, deliberately. `weld_verdict`
    fails OPEN on a probe error because its failure mode is refusing a legitimate label. A
    caller of THIS function is asking "may I treat this as the speaker?", and answering yes on
    a failed probe mints an identity claim from an error. So: probe failure → **False**. The
    conservative answer is always "not the anchor".

    ``schema_name`` is optional: marker 1 simply does not contribute without a bound schema
    (that is exactly why marker 2 exists), and marker 2 still runs.
    """
    if not entity_id:
        return False
    seat = seat_entity_id(schema_name)
    if seat and str(entity_id).strip().lower() == seat:
        return True                       # marker 1 needs no DB read at all
    existing = _existing_labels(cur, entity_id)
    if not existing:                      # None = probe failed, [] = no labels yet
        return False
    return _is_seat_anchor(entity_id, existing, schema_name)


def weld_verdict(cur, entity_id, alias, preference_source,
                 is_preferred=False, schema_name=None):
    """Decide whether registering ``alias`` on ``entity_id`` is an admissible WELD.

    Returns ``(allow: bool, arm: str)``. A refusal is never silent — ``arm`` is what the caller
    logs.

    ALWAYS ALLOWED (in evaluation order):
      * a missing entity or alias;
      * the entity's FIRST label (nothing to co-refer with, so not a weld);
      * re-registering a label the entity already holds (re-ingest stays idempotent);
      * a WARRANTED growth label — the WordNet co-synset / canonical-form lanes, which arrive
        with a recorded licence for the co-reference they assert (see ARM 3);
      * any label at or below the PROVISIONING FLOOR, i.e. one that records no authority claim
        at all (notably ``resolve()``'s origin surface);
      * an explicitly PREFERRED user-stated write. That is a prefLabel assertion, i.e. the
        naming/correction lane, and it must reach the registry's existing user-is-truth
        provenance override and demotion guard rather than being vetoed before them. Measured:
        every corrupting weld on production was ``is_preferred=false``, so this exemption costs
        the guard nothing;
      * the SEAT ANCHOR, unconditionally (see module docstring — refusing there detaches the
        user's own name from the speaker).

    ARM 1 — ``place_takes_no_memory_label``. The USER has asserted the target is a class (a
    class-side edge whose OWN provenance is user-stated) and the incoming label carries MEMORY
    authority. A user surface labelling a user-declared class asserts *name ≡ class*: the
    HARD-LINE category error measured on production, where a pet's given name accumulated as a
    label of its own type node and the name then became a type with instances.

    ARM 2 — ``memory_label_onto_growth_only_node``. The incoming label carries MEMORY authority
    and EVERY label the target already holds was authored by engine growth. Growth may add and
    widen but never re-bind what a user's surface denotes. This arm fires before any ladder
    exists, which is why ARM 1 alone is insufficient.

    ARM 3 — ``unwarranted_growth_coreference``. THREE conditions, all required. NEITHER side
    carries user authority: the incoming label is an engine-growth claim with NO recorded
    warrant, and every label the target already holds is likewise an engine-growth claim. AND
    the target is a PLACE — it occupies a class-side position in this tenant's graph. Measured
    on production, this is the remaining corruption ARMs 1-2 could not see, because it arrived
    below memory authority on both sides: ``{cat} ≡ {<pet name>}``, ``{company} ≡ {<company name>}``,
    ``{old school friend} ≡ {<person name>}``, ``{landing page} ≡ {gauntlet}``,
    ``{issue triage} ≡ {ci investigation} ≡ {tests}`` — extraction inventing a co-reference and
    the index silently collapsing two referents onto one node. The first four are the SAME
    HARD-LINE error ARM 1 exists for (a NAME becoming a label of its own type node); they
    escaped only because the name happened to arrive engine-authored rather than user-stated.

    THE PLACE CONDITION IS LOAD-BEARING AND WAS ADDED ON EVIDENCE, not for tidiness. Without
    it the arm refused 158 of 313 production welds and the surplus was a coherent, legitimate
    family: an engine event/instance node acquiring its own natural phrasing
    (``{occurrence: barbecue party} ≡ {barbecue party}``), an abbreviation
    (``{configuration} ≡ {config}``), an acronym (``{transportation management} ≡ {tms}``), a
    near-synonym (``{primary care provider} ≡ {physician}``). Those are real co-reference on
    nodes that are INSTANCES, not classes — adding a surface to an instance is indexing, and
    losing it costs reachability for nothing. Separating them by string overlap would be fuzzy
    matching, which this engine does not do; the graph separates them exactly, which is the
    same answer the rest of this module reaches — only the graph distinguishes a legitimate
    weld from a corrupting one.

    Why ARM 3 may use placement evidence of ANY provenance where ARM 1 may not: see
    ``_entity_is_place``. ARM 1 declines user truth, so engine evidence there would invert the
    authority order; ARM 3 declines an engine claim, so it cannot.

    Why ARM 3 is the SAFEST of the three, not the boldest: it can only ever decline an
    ENGINE claim. ARMs 1-2 decline a user-stated surface (costing reachability on a name the
    user really said, which is why they need the seat exemption and the evidence-authority
    filter); ARM 3 declines nothing a user ever said, on a node the user never named. It
    cannot violate the authority order in either direction — there is no higher authority
    anywhere in the weld to override, and growth is explicitly permitted to be declined.

    Why it does not close the canonicalization lane — the reason it needed a provenance fix
    rather than a cleverer rule: an engine synonym registered from a lexical resource and an
    extraction-invented co-reference used to be BYTE-IDENTICAL at the write (both
    ``inferred``, no recorded licence), so no rule over the alias table could separate
    warranted from unwarranted co-reference. The fix is upstream: the lexical lane now records
    its warrant in ``preference_source`` (``registry._WARRANTED_SOURCES``), and ARM 3 reads
    that. It buys the lane no extra TRUST — ``lexical`` ranks equal to ``inferred`` — only the
    right to assert co-reference, which is exactly what a lexical resource licenses.
    """
    if not entity_id or not alias:
        return True, ALLOW

    existing = _existing_labels(cur, entity_id)
    if existing is None:                      # probe failed → fail OPEN
        return True, ALLOW
    if not existing:                          # first label: not a weld
        return True, ALLOW
    if alias in {a for a, _ in existing}:     # already held: not a weld
        return True, ALLOW

    incoming_is_memory = _rank(preference_source) >= _memory_tier()

    if not incoming_is_memory:
        # Below memory authority: only ARM 3 can apply, and only to an UNWARRANTED claim
        # authored by growth. A warranted lane, and anything at/below the provisioning floor
        # (no claim recorded), is admitted exactly as before this arm existed.
        if arm3_mode() == "off":
            return True, ALLOW                # ARM 3 disarmed → byte-identical to ARMs 1-2 only
        if not _is_growth_claim(preference_source):
            return True, ALLOW
        # ⚠️ A WARRANT IS NO LONGER A SHORT-CIRCUIT, AND THAT IS THE POINT. It used to return
        # ALLOW here, which made the arm's rollout plan unsound: an OBSERVE run counts REFUSALS,
        # and a weld the warrant wrongly ADMITS is never refused, so it never logs — observe
        # came back clean precisely on the failure mode that matters (a producer-side warrant
        # granted to something that is not an abbreviation definition; see
        # src/extraction/abbreviation.py, "FALSE LICENCES"). The warranted case now falls
        # through the SAME conditions and is logged distinguishably at the decision point, so an
        # observe run measures BOTH directions: what the arm would decline, and what a warrant
        # is carrying past it. It still ALLOWS — the warrant's meaning is unchanged.
        warranted = _warranted(preference_source)
        if _is_seat_anchor(entity_id, existing, schema_name):
            return True, ALLOW                # never re-label the first-person anchor
        if not all(_is_growth_claim(src) for _, src in existing):
            return True, ALLOW
        # The target must ALSO be a PLACE. See ARM 3 in the docstring for why this third
        # condition is load-bearing and why it may use evidence of any provenance.
        if _entity_is_place(cur, entity_id, require_user_evidence=False):
            if warranted:
                # Countable at the SAME level as a refusal, deliberately — this is the leak
                # surface, and a measurement that can only see one direction is not one.
                log_crit(
                    log, "weld_guard.arm3_warrant_admitted",
                    entity=str(entity_id)[:16], alias=str(alias)[:64],
                    preference_source=preference_source, arm=ARM_UNWARRANTED,
                    mode=arm3_mode(),
                    note="ARM 3 would have declined this co-reference onto a PLACE; a RECORDED "
                         "WARRANT admitted it. Sample these — a wrong warrant is the one way "
                         "this arm can be bypassed, and it is invisible in a refusal count.",
                )
                return True, ALLOW
            if arm3_mode() == "enforce":
                return False, ARM_UNWARRANTED
            # OBSERVE: the arm is measuring, not acting. Logged at CRITICAL so the refusal rate
            # is countable in production logs exactly as ARM 1's was, then ALLOW — the write
            # proceeds and ARM 1 keeps enforcing beside it.
            log_crit(
                log, "weld_guard.arm3_would_refuse",
                entity=str(entity_id)[:16], alias=str(alias)[:64],
                preference_source=preference_source, arm=ARM_UNWARRANTED, mode="observe",
                note="ARM 3 in OBSERVE — this unwarranted engine co-reference onto a PLACE is "
                     "being counted, not declined. See the internal design record",
            )
        return True, ALLOW

    if is_preferred:
        return True, ALLOW                    # prefLabel / correction lane (see docstring)

    if _is_seat_anchor(entity_id, existing, schema_name):
        return True, ALLOW                    # the first-person resolution target, never refused

    if _entity_is_user_asserted_place(cur, entity_id):
        return False, ARM_PLACE

    if arm2_enabled() and all(_is_growth_claim(src) for _, src in existing):
        # ARM 2 must satisfy the SAME place condition as ARM 3, with the SAME evidence-authority
        # rule as ARM 1 (user-stated evidence only), because ARM 2 — like ARM 1 — declines a
        # USER-STATED label. See `arm2_enabled` for why this makes the arm provably subsumed.
        if _entity_is_user_asserted_place(cur, entity_id):
            return False, ARM_GROWTH_ONLY

    return True, ALLOW


def refuse_weld(cur, entity_id, alias, preference_source,
                is_preferred=False, schema_name=None) -> bool:
    """Evaluate the guard and report whether the caller must SKIP this alias write.

    Returns True only in ``enforce`` mode on a refusal — which IS the module default (see
    ``_DEFAULT_MODE``). In ``observe`` the verdict is logged and False is returned,
    so the write path is byte-identical to the pre-guard behaviour while the refusal rate is
    measured; ``off`` skips the probes entirely. (This docstring said observe was "the default";
    it has not been since the enforce rollout, and a reader taking that at face value would
    believe a refusal here cannot skip a write. It can, and does.)
    """
    mode = guard_mode()
    if mode == "off":
        return False
    allow, arm = weld_verdict(cur, entity_id, alias, preference_source,
                              is_preferred=is_preferred, schema_name=schema_name)
    if allow:
        return False
    log_crit(
        log,
        "weld_guard.refused" if mode == "enforce" else "weld_guard.would_refuse",
        entity=str(entity_id)[:16],
        alias=str(alias)[:64],
        preference_source=preference_source,
        arm=arm,
        mode=mode,
        note="a label carrying USER authority was about to be welded onto an entity it does "
             "not denote. The surface keeps its OWN entity — nothing is deleted, the "
             "co-reference claim is declined. See the internal design record",
    )
    return mode == "enforce"
