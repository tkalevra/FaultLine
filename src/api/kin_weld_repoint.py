"""KIN WELD-ENTITY REPOINT - the ingest-side half of SPINE_ROLE_HEAD_NO_BIND
(fog-family lab F1-R2/R3, DEV/FINDINGS-fog-family-lab-20261107.md finding F1).

WHY THIS EXISTS: the deriver fix ("src/extraction/linguistics.py") binds the PROPER NAME on
the kin copula frame, but a seat that ALREADY holds role-weld entities from the pre-fix era
keeps them: an entity whose entire identity is the kin role word ("wife" - its only alias)
carrying a LIVE (user, spouse, weld-entity) row. Recall keeps rendering "You and Wife are
spouses/partners" from that ghost row forever, and a weld-shaped edge (user, spouse,
role-word-surface) CONFIRMS it (confirmed_count climbing) instead of dying.

LIVE DIRECTION (critic R3, R2-B-1): the stored pipeline rows are (user, spouse, NAME) -
SUBJECT=user, OBJECT=name - and the /ingest door's kin edges may arrive either direction
(the deriver's own surfaces are (name, kin_rel, user); the commit inverts). Every lookup in
this module is therefore BIDIRECTIONAL: the bind signature accepts the kin edge touching the
name on EITHER endpoint, the committed-row lookup matches subject OR object, and the rival
supersede retires both arrangements of the ghost pair. The R2 pins encoded the inverse
direction and were hermetic-only - fixed here and in the pins.

THE RETIRE RULE: when a committed kinship-rel row touches the named entity AND the batch
carries the naming-chain bind signature (an also_known_as edge from the name to a kinship
role word; metadata-gated via the per-tenant kinship_noun cue class + kinship rel map, NO
word list), every RIVAL entity that (a) holds one of those role aliases, (b) holds NO alias
outside the kinship role class (THE PURE ROLE-WELD SIGNATURE: a real named person always
carries a non-role alias, so Diane-with-a-name is never touched), and (c) carries a LIVE row
of the SAME kin rel to the SAME counterpart (the user side of the named bind, either
arrangement), gets its kin row SUPERSEDED and the role alias DELETED - the weld entity
archived with the alias transferred to the named person (the naming chain's own alias leg
already registered the role on the named entity, so the transfer is complete after the
delete).

THE RESOLUTION GATE (critic R3, R2-B-2) - used by _ingest_impl's validation loop, never at
derive time: never-seen kin-copula names must mint/bind their OWN entity instead of being
absorbed by role-slot entities, and weld entities must be confirm-magnetic-NO targets for
kin binds:
  - kin_name_surface_taint: the NAME surface of a bind pair must not alias-resolve onto a
    PURE role-weld entity, nor onto an entity carrying a live row of a DIFFERENT kinship rel
    (the Rosalind-on-the-mother-entity wound: the seat's seed had aliased rosalind onto the
    user's mother; a wife bind resolved straight onto her). Tainted -> the caller mints a
    FRESH named entity (registry.resolve(force_mint_fresh=True)).
  - kin_role_object_on_weld: a weld-shaped kin edge (object surface IS the role word)
    resolving onto a PURE role-weld entity is refused by the caller - the ghost row is never
    confirmed (confirmed_count stops climbing) and never strengthened. A declined confirm
    lands nothing - honest absence - until a real named bind lands and the retire rule above
    heals the seat.

SCOPE GUARD (coordinate-by-avoidance): the kinship cue classes ONLY. The pet lane (has_pet -
Lane D's F5) never matches; non-kin possessed roles (manager -> <role>_of, owner opt-in)
never match; a rival that carries any non-kinship alias (any real name) is never touched;
the rival's OTHER rows are never touched - the supersede is keyed on (same kin rel, same
counterpart, both arrangements).

Pure helpers (collect_kin_name_binds, rival_is_pure_role_weld) are hermetically pinned in
tests/api/test_kin_weld_repoint.py together with fake-cursor exercises of the retire pass in
BOTH row arrangements, the taint gate, and the weld-object gate.
Fail-open, SAVEPOINT-guarded, flag-gated (SPINE_ROLE_HEAD_NO_BIND, default ON).
"""
import logging

log = logging.getLogger("uvicorn.error")

_ALIAS_SYNC_REL = "also_known_as"


def collect_kin_name_binds(edges, kin_relmap: dict, kin_nouns: frozenset) -> list:
    """The naming-chain bind signature, from the request's ORIGINAL-SURFACE edges.

    A bind is (name_surface, role_surface, kin_rel) where
      - an edge files also_known_as -> role_surface in the kinship_noun cue class, AND
      - an edge files the role's MAPPED kin rel (kin_relmap[role]) TOUCHING the same name
        surface on EITHER endpoint (the live pipeline inverts the deriver's (name, rel, user)
        surfaces into (user, rel, name) rows - both shapes pass).
    Pure; metadata-driven; no in-code noun list. Returns a deduped list.
    """
    if not edges:
        return []
    kin_rels = {str(v or "").strip().lower() for v in kin_relmap.values() if v}
    binds: list = []
    seen: set = set()
    kin_edges = [(str(getattr(e, "subject", "") or "").strip().lower(),
                  str(getattr(e, "object", "") or "").strip().lower(),
                  str(getattr(e, "rel_type", "") or "").strip().lower())
                 for e in edges]
    for e in edges:
        rel = str(getattr(e, "rel_type", "") or "").strip().lower()
        if rel != _ALIAS_SYNC_REL:
            continue
        name = str(getattr(e, "subject", "") or "").strip().lower()
        role = str(getattr(e, "object", "") or "").strip().lower()
        if not name or not role or role not in kin_nouns or name == role:
            continue
        kin_rel = str(kin_relmap.get(role, "") or "").strip().lower()
        if not kin_rel or kin_rel not in kin_rels:
            continue
        # the kin edge: same rel, TOUCHING the name on either endpoint (live direction is
        # (user, kin_rel, name); the deriver's own surfaces are (name, kin_rel, user)).
        touched = any(
            rel_x == kin_rel and name in (subj_x, obj_x) and subj_x != obj_x
            for subj_x, obj_x, rel_x in kin_edges)
        if not touched:
            continue
        key = (name, role, kin_rel)
        if key in seen:
            continue
        seen.add(key)
        binds.append(key)
    return binds


def rival_is_pure_role_weld(aliases, kin_nouns: frozenset) -> bool:
    """THE PURE ROLE-WELD SIGNATURE: every alias of the entity is a kinship role word.

    A real named person ("seraphina" + "wife") carries a non-role alias -> False, never touched
    (multi-name persons are exactly what the walk's slot-alias test resolves by). A pre-fix weld
    entity ("wife" alone, or "wife" + "spouse") -> True. Pure; pinned.
    """
    toks = {str(a or "").strip().lower() for a in (aliases or [])}
    toks.discard("")
    if not toks:
        return False
    return all(t in kin_nouns for t in toks)


def _alias_hit(db, surface: str, user_entity_id: str):
    """The alias row resolve() would return (same ORDER BY as the registry's probe)."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT entity_id FROM entity_aliases "
            "WHERE lower(alias) = %s "
            "ORDER BY CASE WHEN entity_id = %s THEN 0 ELSE 1 END, is_preferred DESC "
            "LIMIT 1",
            (str(surface or "").strip().lower(), user_entity_id))
        row = cur.fetchone()
    return str(row[0]) if row else None


def _entity_aliases(db, entity_id: str) -> set:
    with db.cursor() as cur:
        cur.execute("SELECT lower(alias) FROM entity_aliases WHERE entity_id = %s", (entity_id,))
        return {str(r[0] or "").strip().lower() for r in cur.fetchall()}


def kin_name_surface_taint(db, surface: str, kin_rel: str, kin_nouns: frozenset,
                           kin_rel_values, user_entity_id: str):
    """R2-B-2 gate (prong A): must the NAME surface of a kin-bind pair mint FRESH?

    Returns None when the alias-hit (if any) is a safe target - a genuine named entity with no
    rival kinship row - and a taint dict when the surface would be absorbed:
      {"taint": "pure_role_weld"}                       - the alias-hit entity's ENTIRE alias
                                                          set is kinship role words (a weld);
      {"taint": "kin_conflict", "conflicting_rel": X}   - the alias-hit entity carries a LIVE
                                                          row of a DIFFERENT kinship rel to the
                                                          user (Rosalind-on-the-mother: a wife
                                                          bind cannot resolve onto the entity
                                                          that is already the user's mother).
    Read-only; fail-open (any error -> None -> normal resolve)."""
    try:
        hit = _alias_hit(db, surface, user_entity_id)
        if hit is None:
            return None
        if rival_is_pure_role_weld(_entity_aliases(db, hit), kin_nouns):
            return {"taint": "pure_role_weld", "entity_id": hit}
        rels = {str(r or "").strip().lower() for r in (kin_rel_values or set())}
        rels.discard("")
        with db.cursor() as cur:
            cur.execute(
                "SELECT rel_type FROM facts "
                "WHERE rel_type = ANY(%s) AND rel_type <> %s AND superseded_at IS NULL "
                "AND ((subject_id = %s AND object_id = %s) "
                "     OR (subject_id = %s AND object_id = %s)) LIMIT 1",
                (sorted(rels), str(kin_rel), hit, user_entity_id,
                 user_entity_id, hit))
            row = cur.fetchone()
        if row:
            return {"taint": "kin_conflict", "entity_id": hit,
                    "conflicting_rel": str(row[0])}
        return None
    except Exception as exc:  # noqa: BLE001 - the gate never blocks a resolution
        # STDLIB-SAFE (critic R5 R4-3): this module logs via logging.getLogger(
        # "uvicorn.error") — structlog-style kwargs on it raise TypeError INSIDE the except
        # handler, and the swallowed error surfaces to main.py's catch as a clean
        # kin_weld_repoint_skipped. Every log call here is positional so the real error
        # text reaches the record (a swallowed except must at minimum log the real error).
        log.warning("ingest.kin_name_taint_gate_failed surface=%s error=%s",
                    str(surface)[:40], str(exc)[:140])
        return None


def kin_role_object_on_weld(db, surface: str, kin_nouns: frozenset) -> bool:
    """R2-B-2 gate (prong B): would this weld-shaped kin edge CONFIRM a pure role-weld entity?

    True when the object surface (a kinship role word) alias-resolves onto an entity whose
    entire alias set is kinship role words - the ghost row that must never be confirmed again
    (confirm-magnetic-NO). Read-only; fail-open False."""
    try:
        hit = _alias_hit(db, surface, "user")
        if hit is None:
            return False
        return rival_is_pure_role_weld(_entity_aliases(db, hit), kin_nouns)
    except Exception as exc:  # noqa: BLE001 - the gate never blocks a resolution
        log.warning("ingest.kin_weld_object_gate_failed surface=%s error=%s",  # stdlib-safe
                    str(surface)[:40], str(exc)[:140])
        return False


def kin_bind_registry_type(db, entity_id: str):
    """F1-R4f (critic R4-1): read a just-minted entity's type ON THE PASSED CONNECTION.

    The R3-2a override first read through the WGM gate's OWN connection — which cannot see
    the ingest transaction's UNCOMMITTED mint (and a stale open snapshot on that conn misses
    it forever), read None, failed open silently, and the spouse bind refused on the passed
    turn-local GLiNER type while the minted registry row said Person (the Odalys wound).
    The ingest txn's own cursor sees BOTH: its own uncommitted writes, and other
    connections' committed ones (READ COMMITTED per-statement snapshot). Read-only; None on
    any failure (the caller fails open)."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT entity_type FROM entities WHERE id = %s", (str(entity_id),))
            row = cur.fetchone()
        return (str(row[0]).strip() or None) if row else None
    except Exception as exc:  # noqa: BLE001 - the override never blocks a bind
        log.warning("ingest.kin_bind_registry_type_failed conn=%s entity=%s error=%s",
                    type(db).__name__ if db is not None else "None",
                    str(entity_id)[:16], str(exc)[:140])
        return None


def kin_bind_type_wins(registry_type, turn_local_type) -> bool:
    """Pure decision: must the minted REGISTRY row type override the turn-local declaration
    for a kin bind? Person row vs non-Person declaration -> the registry row wins. Anything
    else (no row, types agree, non-Person row) -> keep the declaration (fail-open)."""
    return bool(
        registry_type and str(registry_type).strip().lower() == "person"
        and turn_local_type and str(turn_local_type).strip().lower() != "person")




_KIN_REL_FALLBACK_WORDS = frozenset({
    "spouse", "wife", "husband", "mother", "father",
    "son", "daughter", "brother", "sister"})

# R13 (critic structural verdict — the live-lane wiring audit): the KIN SINGLE-VALUED
# OVERLAY. The seed's rel_types metadata ships spouse is_single_valued=False (verified on
# the live DB), which gated the R12 retire-incumbents discipline OFF on the live lane —
# the exact zombie-class shape (code+pins true, traffic bypasses). The kin copula slot is
# single-valued BY CONSTRUCTION (owner doctrine: ONE live spouse), so the overlay declares
# it here, next to the guard words, independent of seed data. Multi-valued kin rels
# (children, siblings) and the pet lane stay metadata-gated (untouched). The R12h live
# smoke is the wiring gate: plant an incumbent -> bind -> the incumbent MUST retire.
KIN_SINGLE_VALUED_SLOTS = frozenset({"spouse"})


def kin_copula_person_assertion(surface, kin_nouns) -> bool:
    """R14 (critic RG-A2 regression — the Slate class): a kin-copula complement is a
    USER-STATED PERSON ASSERTION when the surface is NAME-SHAPED. The R7 doctrine ('X is
    a person' is user-stated truth; it flips the stale annotation) applies to the frame
    itself: 'My wife is Slate.' asserts Slate is a person, whatever GLiNER's Object-typing
    said — the frame IS the type assertion, ahead of the accepted-types check.

    Name-shaped = a surface whose first alphabetic char is UPPERCASE and which is not a
    kin role word. 'granite' (lowercase) and 'wife' (a role word) never assert — the
    honest-decline path stands exactly as pinned since R5-2.

    THE DISPLAY-FORM SECOND RUNG (BUG-FAMILY-9, the child-entity type refusal, 2026-11-13):
    the R5-2 contract keys on the casing THE USER ACTUALLY WROTE, but the deriver's
    surfaces are pipeline-lowercased (the spine's load-bearing `.strip().lower()` invariant)
    — so a kin-frame name the user wrote capitalized ("My <kin role> Wren started
    kindergarten" — the measured repro) arrives as 'wren', fails the name-shape check, and
    the bind endpoint keeps the turn-local GLiNER type (the bird word 'wren' typed ANIMAL)
    — the child_of edge is then refused (head ANIMAL vs the declared Person-only roles) and
    the kids cannot be stored. Migration 214's observed-display overlay RETAINS that
    verbatim casing from the raw turn at the ingest boundary (PROPN runs only,
    sentence-initial evidence discarded — see src/extraction/display_case.py). When the
    carried surface declined but its OBSERVED display form is name-shaped, the SAME
    contract asserts on the same signal the contract was written against: the user's own
    capitalization. No overlay entry → None → the decline stands byte-for-byte ('granite'
    written lowercase is never a PROPN run; an unbound ContextVar — every direct unit
    caller — behaves identically to the pre-rung function)."""
    s = str(surface or "").strip()
    if len(s) < 2:
        return False
    first = next((ch for ch in s if ch.isalpha()), "")
    if not first or not first.isupper():
        # SECOND RUNG: the carried surface lost its casing in the pipeline; the observed
        # display form (the user's verbatim turn) is the surviving casing signal.
        try:
            from src.extraction.display_case import display_form_for
            observed = display_form_for(s.lower())
        except Exception:  # noqa: BLE001 — fail-safe: the decline stands
            return False
        if not observed:
            return False
        return kin_copula_person_assertion(observed, kin_nouns)
    nouns = {str(n or "").strip().lower() for n in (kin_nouns or ())}
    return s.lower() not in nouns


def kin_rel_guard_set(kin_rel_values, kin_rel_map) -> frozenset:
    """R8 HOTFIX (critic RG-0): THE merged kin-guard set — canonical rel values + the RAW
    surface words the spine may emit (the relmap's keys) + the fallback kin words. Computed
    ONCE at gate arming; guard sites do plain membership. One container, one type: the
    guard must never union mixed containers at request time (the dict|set TypeError on the
    R8 cook killed every statement txn, 12/12)."""
    vals = {str(v or "").strip().lower() for v in (kin_rel_values or ())}
    vals.update(str(k or "").strip().lower() for k in (kin_rel_map or {}))
    vals.update(_KIN_REL_FALLBACK_WORDS)
    vals.discard("")
    return frozenset(vals)

def kin_bind_endpoint(subject, object_, user_entity_id):
    """R7 (critic R5-3): the bind entity is the NON-USER endpoint of a kinship-rel row —
    whichever side the name landed on (R3 bidirectional doctrine, in ID form). The deriver
    surfaces are NAME-ON-SUBJECT ((foxglove, spouse, user)); the live door stores
    NAME-ON-OBJECT ((user, spouse, foxglove)) — the adjudication must key on either."""
    u = str(user_entity_id or "")
    s = str(subject or "")
    o = str(object_ or "")
    if s and s != u:
        return s
    if o and o != u:
        return o
    return None


def kin_self_role_surface(subject_surface, object_surface, user_surface, kin_nouns):
    """R7/R8 (critic R5-2 + RG-B(2), tautology gate): refuse ONLY when a kinship ROLE WORD
    rides the NAME slot — never when it sits in the grammatical predicate slot.
    (a) the SUBJECT surface is a role word: the name slot itself is junk ((wife,spouse,user),
        'My wife is Wife.' inverted) — refuse both arrangements.
    (b) the SUBJECT is the USER and the OBJECT surface is a role word: 'my wife is Wife.'
        — the object is the would-be bind name and it is a role word — refuse.
    (c) 'Katya is my wife.' — the INVERSE-COPULA family (queued since R1): the subject is a
        genuine name and the role phrase sits in the predicate; that is a legitimate bind
        SHAPE and must reach the adjudication, never be refused here."""
    u = str(user_surface or "").strip().lower()
    s = str(subject_surface or "").strip().lower()
    o = str(object_surface or "").strip().lower()
    if s and s in kin_nouns:
        return s
    if o and o in kin_nouns and s == u and u:
        return o
    return None


_REPAIR_TYPE_VOCAB = frozenset({"person", "human", "a person", "a human"})


def kin_repair_type_assertion(rel_type, object_surface) -> bool:
    """R7 (critic R5-5): the INSTRUCTED type repair. The refusal message itself teaches
    '<name> is a person' — that assertion is user-stated truth and outranks the stale GLiNER
    annotation (RDFS doctrine: an assertion never loses to an annotation). Only the
    type-assertion rel classes with the taught vocabulary count."""
    rt = (rel_type or "").strip().lower()
    if rt not in {"instance_of", "is_a"}:
        return False
    surface = str(object_surface or "").strip().lower().rstrip(".")
    return surface in _REPAIR_TYPE_VOCAB


_DOC_AUTHORITY_SOURCES = frozenset({
    "doc", "document", "documents", "staged", "derived", "extraction", "ingest",
    "doc_extraction", "document_ingest"})


def kin_weld_slot_refusal_core(aliases, alias, preference_source, kin_nouns):
    """Pure core of kin_weld_slot_refusal — alias set in, arm name or None out. Pinned."""
    toks = {str(a or "").strip().lower() for a in (aliases or [])}
    toks.discard("")
    a = str(alias or "").strip().lower()
    if not toks or not a:
        return None
    if a not in kin_nouns and all(t in kin_nouns for t in toks):
        return "person_onto_pure_weld"
    src = str(preference_source or "").strip().lower()
    if (a in kin_nouns and any(t not in kin_nouns for t in toks)
            and src in _DOC_AUTHORITY_SOURCES):
        return "role_word_onto_person_from_doc"
    return None


def kin_weld_slot_refusal(db, entity_id, alias, preference_source, kin_nouns):
    """R8 ADDENDUM (Lane A's RG-F root trace): alias accretion across the WELD BOUNDARY.

    The weld entity is a ROLE SLOT, not a person — the slot may absorb more role words, but a
    PERSON name accreting onto it ('magnus' onto the wife-weld) is what later makes a
    role-collision READABLE as a person binding. Conversely a role word accreting onto a
    PERSON entity from DOC authority ('wife' landing on the magnus entity from the fenland
    doc) contaminates the person slot — the bind-side tautology doctrine, at the linking seam.

    Arms (return the arm name when the alias write must be refused):
    - person_onto_pure_weld — the target's ENTIRE alias set is role words and the incoming
      alias is not one: refuse regardless of source (mint a fresh person entity instead).
    - role_word_onto_person_from_doc — the incoming alias is a role word, the target carries
      non-role (person) aliases, and the source authority is DOC/derived: refuse (user
      authority keeps its documented transfer path — the R3 reconcile writes role aliases
      onto named binds through its own SQL and never passes here).
    Returns None (allow) on anything else; fail-open None on any probe error."""
    try:
        return kin_weld_slot_refusal_core(
            _entity_aliases(db, str(entity_id)), alias, preference_source, kin_nouns)
    except Exception:
        return None


def kin_weld_entity_on_weld(db, entity_id: str, kin_nouns: frozenset) -> bool:
    """R4 (R3-1) ID-keyed confirm-magnetic-NO gate: is this RESOLVED ENTITY a pure role-weld?

    The live pipeline rewrites surfaces to UUIDs at EXTRACTION
    (ingest.subject_resolved_at_extraction), so surface-keyed weld checks never see the role
    word; this gate keys on the resolved entity's ENTIRE alias set via entity_aliases. Read-
    only; fail-open False."""
    try:
        return rival_is_pure_role_weld(_entity_aliases(db, str(entity_id)), kin_nouns)
    except Exception as exc:  # noqa: BLE001 - the gate never blocks a landed row
        log.warning("ingest.kin_weld_entity_gate_failed entity=%s error=%s",  # stdlib-safe
                    str(entity_id)[:16], str(exc)[:140])
        return False


def _retire_id_binds(db, binds, kin_nouns: frozenset) -> int:
    """Shared rival-retire inner over ID-form binds [(name_uuid, name_roles, kin_rel)].

    BIDIRECTIONAL row lookup; rivals = pure role-weld entities holding one of the bind's role
    aliases; ghost kin row superseded BOTH arrangements + role alias transferred. Fail-open."""
    retired = 0
    try:
        with db.cursor() as cur:
            for name_uuid, name_roles, kin_rel in binds:
                # the named bind's committed kin row -> the COUNTERPART (the user side) the
                # supersede is keyed on. BIDIRECTIONAL: the row may be (user, rel, name) or
                # (name, rel, user).
                try:
                    cur.execute(
                        "SELECT subject_id, object_id FROM facts "
                        "WHERE rel_type = %s AND (subject_id = %s OR object_id = %s) "
                        "AND superseded_at IS NULL LIMIT 1",
                        (kin_rel, name_uuid, name_uuid))
                    krow = cur.fetchone()
                except Exception:  # noqa: BLE001 - table missing (unmigrated tenant) -> no-op
                    continue
                if krow is None:
                    continue  # nothing committed for this bind -> nothing to reconcile
                subj_id, obj_id = str(krow[0]), str(krow[1])
                counterpart = obj_id if subj_id == name_uuid else subj_id
                # rivals: entities holding one of the named bind's role aliases, other than the
                # named person itself. The PURE-WELD filter runs in Python against each rival's
                # full alias set (a real named person is never touched).
                cur.execute(
                    "SELECT DISTINCT entity_id FROM entity_aliases "
                    "WHERE lower(alias) = ANY(%s) AND entity_id <> %s",
                    (sorted(name_roles), name_uuid))
                for (rival_uuid,) in cur.fetchall():
                    rival_uuid = str(rival_uuid)
                    rival_aliases = _entity_aliases(db, rival_uuid)
                    if not rival_is_pure_role_weld(rival_aliases, kin_nouns):
                        continue
                    try:
                        cur.execute("SAVEPOINT sp_kin_weld_repoint")
                        # (1) the ghost kin row - superseded by the named bind (recall filters
                        #     superseded_at IS NULL, so "You and Wife" stops rendering). Both
                        #     arrangements of the pair die.
                        cur.execute(
                            "UPDATE facts SET superseded_at = NOW() "
                            "WHERE rel_type = %s AND superseded_at IS NULL "
                            "AND ((subject_id = %s AND object_id = %s) "
                            "     OR (subject_id = %s AND object_id = %s))",
                            (kin_rel, counterpart, rival_uuid, rival_uuid, counterpart))
                        retired += cur.rowcount
                        # (2) the role alias - TRANSFERRED (the named entity already holds it),
                        #     so no future role-word resolution can re-absorb into the dead
                        #     weld entity.
                        cur.execute(
                            "DELETE FROM entity_aliases WHERE entity_id = %s "
                            "AND lower(alias) = ANY(%s)",
                            (rival_uuid, sorted(name_roles)))
                        cur.execute("RELEASE SAVEPOINT sp_kin_weld_repoint")
                        # stdlib-safe positional (critic R5 R4-3): the kwargs form raised
                        # TypeError on the LIVE uvicorn logger (INFO configured there) and ate
                        # every successful repoint into the outer catch.
                        log.info(
                            "ingest.kin_weld_entity_repointed rival=%s name_entity=%s "
                            "kin_rel=%s role_alias=%s note=%s",
                            str(rival_uuid)[:16], str(name_uuid)[:16], kin_rel,
                            sorted(name_roles),
                            "pre-fix role-weld entity superseded by the named bind; "
                            "kin row archived, role alias transferred")
                    except Exception as _spe:  # noqa: BLE001 - one rival never sinks the rest
                        cur.execute("ROLLBACK TO SAVEPOINT sp_kin_weld_repoint")
                        log.warning("ingest.kin_weld_repoint_rival_failed rival=%s error=%s",
                                    str(rival_uuid)[:16], str(_spe)[:160])
    except Exception as exc:  # noqa: BLE001 - fail-open: the ingest is never blocked by this
        log.warning("ingest.kin_weld_repoint_failed error=%s", str(exc)[:160])
        return retired
    return retired


def retire_rival_weld_entities(db, edges, kin_relmap: dict, kin_nouns: frozenset) -> int:
    """Supersede rival pure-role-weld kin rows + transfer the role alias, for every kin-name
    bind in this request's SURFACE edge batch (hermetic/pin lane). The LIVE call site uses
    retire_rival_weld_entities_resolved — the extraction phase pre-resolves surfaces to UUIDs,
    so surface edges are dark there (critic R3/R4). BIDIRECTIONAL (R3). Returns the number of
    superseded kin rows."""
    binds = collect_kin_name_binds(edges, kin_relmap, kin_nouns)
    if not binds:
        return 0
    id_binds = []
    try:
        with db.cursor() as cur:
            for name, role, kin_rel in binds:
                # the NAMED entity: the alias the naming chain registered this turn.
                try:
                    cur.execute(
                        "SELECT entity_id FROM entity_aliases WHERE lower(alias) = %s "
                        "ORDER BY is_preferred DESC LIMIT 1",
                        (name,))
                    row = cur.fetchone()
                except Exception:  # noqa: BLE001 - table missing (unmigrated tenant) -> no-op
                    continue
                if row is None:
                    continue
                name_uuid = str(row[0])
                # the role aliases in play: the bind signature's role (the alias leg may have
                # been authority-dropped on the /ingest lane) UNION whatever kin-role aliases
                # the named entity already carries.
                cur.execute(
                    "SELECT lower(alias) FROM entity_aliases WHERE entity_id = %s",
                    (name_uuid,))
                name_aliases = {str(r[0] or "").strip().lower() for r in cur.fetchall()}
                name_roles = ({role} | {a for a in name_aliases if a in kin_nouns}) - {name}
                if name_roles:
                    id_binds.append((name_uuid, frozenset(name_roles), kin_rel))
    except Exception as exc:  # noqa: BLE001 - fail-open (surface lane)
        log.warning("ingest.kin_weld_repoint_failed error=%s", str(exc)[:160])
        return 0
    return _retire_id_binds(db, id_binds, kin_nouns)


def retire_rival_weld_entities_resolved(db, resolved_rows, kin_rel_values, kin_nouns: frozenset,
                                        user_entity_id) -> int:
    """F1-R4 (critic R3-1): the retire pass keyed on the POST-RESOLUTION LANDED STATE.

    Every resolved Class-A row whose rel is a kinship rel and whose NON-USER endpoint carries a
    kin-role alias (the naming-chain bind signature in ID form) reconciles its pure role-weld
    rivals. Keys on entity IDs + alias tables — the surfaces are gone by the time rows land
    (extraction-time UUID resolution). The pet lane, non-kin roles and any entity carrying a
    real-name alias never match. SAVEPOINT-guarded, fail-open. Returns superseded kin rows."""
    user_uid = str(user_entity_id or "")
    binds = []
    seen = set()
    for row in resolved_rows or []:
        try:
            subj = str(row[1] or "")
            obj = str(row[2] or "")
            rel = str(row[3] or "").strip().lower()
        except Exception:  # noqa: BLE001 - tolerate odd row shapes
            continue
        if rel not in (kin_rel_values or set()):
            continue
        if subj == obj:
            continue
        counterpart = None
        if subj == user_uid:
            counterpart = obj
        elif obj == user_uid:
            counterpart = subj
        else:
            continue  # third-party row — not this user's bind
        try:
            aliases = _entity_aliases(db, counterpart)
        except Exception:  # noqa: BLE001
            continue
        roles = {a for a in aliases if a in kin_nouns} - {counterpart}
        if not roles:
            continue  # no kin-role alias on the landed endpoint -> not a naming-chain bind
        key = (counterpart, rel)
        if key in seen:
            continue
        seen.add(key)
        binds.append((counterpart, frozenset(roles), rel))
    if not binds:
        return 0
    return _retire_id_binds(db, binds, kin_nouns)
    return _retire_id_binds(db, binds, kin_nouns)


def kin_retire_slot_incumbents(db, user_entity_id, rel_type, bind_entity_id) -> int:
    """F1-R12 (critic RG-Z-2): a valid-lane kin bind on a NAMED entity supersedes the
    incumbent live rows of the SAME single-current kin slot — a spouse is ONE-LIVE by
    construction: the residence lane's single-current discipline extended to the kin
    copula at the moment the bind lands, not after the age-ranked ghost sweep.

    Retires (archive-not-annihilate: superseded_at set, the row kept and recoverable)
    every live AFFIRMED fact of the slot in EITHER orientation whose other endpoint is
    NOT the fresh bind: (user, rel, other) and (other, rel, user). Third-party rows
    (neither endpoint the user) never match; negated rows keep their own gate (a denial
    is its own record, never a supersede casualty); the fresh bind's own row cannot
    match itself.

    CALLER-GATED: the call site must fire only for kin rels (kin_rel_guard) whose
    rel metadata declares is_single_valued (pets and other multi-valued rels keep the
    coexist discipline), and only when the bind endpoint is a NAMED entity. Failure
    rolls back to the savepoint and returns 0 — the bind itself is never blocked."""
    uid = str(user_entity_id or "")
    ent = str(bind_entity_id or "")
    rel = str(rel_type or "").strip().lower()
    if not uid or not ent or not rel or uid == ent:
        return 0
    try:
        with db.cursor() as cur:
            cur.execute("SAVEPOINT sp_kin_incumbent")
            cur.execute(
                "UPDATE facts SET superseded_at = COALESCE(superseded_at, now()), "
                "updated_at = now() "
                "WHERE rel_type = %s AND superseded_at IS NULL AND archived_at IS NULL "
                "AND COALESCE(polarity, 'affirmed') = 'affirmed' "
                "AND ((subject_id = %s AND object_id IS DISTINCT FROM %s) "
                "  OR (object_id = %s AND subject_id IS DISTINCT FROM %s))",
                (rel, uid, ent, uid, ent))
            retired = int(cur.rowcount or 0)
            cur.execute("RELEASE SAVEPOINT sp_kin_incumbent")
        return retired
    except Exception:
        try:
            with db.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT sp_kin_incumbent")
                cur.execute("RELEASE SAVEPOINT sp_kin_incumbent")
        except Exception:
            pass
        return 0
