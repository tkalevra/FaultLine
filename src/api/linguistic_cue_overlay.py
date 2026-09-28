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

import structlog

# Reuse the SAME request-schema ContextVar binding as the rel_type/taxonomy/temporal overlays so ONE
# set_current_schema()/reset_current_schema() per request governs ALL overlays.
from src.api import rel_type_overlay
from src.api.db_read import read_only_connection

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

# ── BOOTSTRAP NAMING-NOUN set (category='naming_noun') — DB-DOWN SAFETY NET ONLY ─────
# The NOMINAL half of the naming construction: the head noun of a copular naming frame whose
# complement IS a name — "<bearer>'s NAME is X", "her NICKNAME is X", "the ALIAS of my sister is X".
# Distinct from ``naming_verb`` (the predicative class: call/dub/christen) and from
# ``alias_predicate`` (the phrasal class: go BY, known AS): those predicate the naming with a VERB,
# this one carries it in the SUBJECT NP of a copula.
#
# WHY IT IS ITS OWN CLASS AND NOT A REUSE OF ``naming_verb``. The two sets overlap in surface form
# (name/nickname/title/label/term are both verbs and nouns) but NOT in membership: ``alias`` and
# ``surname`` are naming NOUNS with no verbal use in this frame, while ``call``/``dub``/``christen``
# are naming VERBS whose noun readings are not naming nouns at all ("my sister's call"). Reusing the
# verb set would have been both over- and under-inclusive, in a class whose consumer decides whether
# a real person's name may be written onto a different real person.
#
# ⚠️ THE CONSUMER'S FAILURE MODE IS ASYMMETRIC, AND THAT SETS THE MEMBERSHIP BIAS. This class is read
# by the speaker-rename refusal (``linguistics.naming_frame_third_party_profile`` →
# ``main.correct_fact``). A member that should not be here costs at most a CLARIFICATION QUESTION on
# a correction that would have landed on the speaker anyway; a member MISSING from here costs a third
# party's name written onto the speaker's own entity — unrecoverable corruption of user truth. So
# seed inclusively within the evidenced class and grow it per-tenant on the same rail.
# Mirrors migration 270's public seed. This in-code set is the code-fallback seed only, NOT the
# authority.
_BOOTSTRAP_NAMING_NOUNS: frozenset[str] = frozenset({
    "name", "nickname", "alias", "moniker", "surname", "byname", "epithet",
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

# ── BOOTSTRAP CESSATIVE verb set + polysemy MODE map — DB-DOWN SAFETY NET ONLY ───────
# CESSATIVE ASPECT — "aspect that expresses the cessation of an event or state" (SIL International,
# *Glossary of Linguistic Terms*, "Cessative Aspect", https://glossary.sil.org/term/cessative-aspect;
# hierarchy Grammatical Category > Aspect > Cessative Aspect; its polar opposite, Inchoative Aspect,
# is the sibling `inchoative_verb` class). The VERB class itself is standardly called the *aspectual
# verbs* / *aspectualizers* (Freed, Alice F. 1979, *The Semantics of English Aspectual
# Complementation*, Reidel, DOI 10.1007/978-94-009-9475-1). The class is named for the ASPECT because
# that is the term with a citable primary definition behind it: "terminative aspect" has NO entry in
# the SIL glossary and could not be sourced (see perfect-flow SPEC.md §D8).
#
# WHY A NEW CLASS AND NOT `aspectual_control_verb`: that class's floor DELIBERATELY MIXES ingressive,
# continuative and terminative phase verbs for a DIFFERENT job — licensing descent into a progressive
# complement — and its own comment records that the inchoative and aspectual-control rails are kept
# distinct for exactly this reason. Reusing it would make "I KEPT emailing Tom" a cessation.
#
# `description` carries the ADMITTED COMPLEMENT SHAPES — a `|`-joined subset of
# {xcomp_progressive, direct_object, intransitive}, the three shapes
# `linguistics.analyze_directive` reports. This is the same set+map-on-one-rail shape
# `exemplification_marker` uses for its polysemy mode, resolved by `resolve_cessative_verb_shapes`.
# It is per-member because the polysemy is per-member and shape-specific:
#   * "I STOPPED the car" / "I DROPPED my phone" / "I FOLDED the laundry" are ordinary transitives,
#     so those members do NOT admit `direct_object`.
#   * "the tibberow FOLDED" / "he RETIRED" ARE cessations, so `fold`/`retire` DO admit
#     `intransitive`.
# A member's cue lemma alone NEVER routes destructive: the GRAMMATICAL corroboration at the call
# site (main-clause declarative ROOT, overt subject, not negated, not interrogative, one of the
# three shapes) must ALSO hold, and the shape must be in this member's admitted set.
# DB-HELD + per-tenant + GROWABLE on the SAME rail (category='cessative_verb'); this in-code set is
# the DB-DOWN code-fallback seed only, NOT the authority.
_CESSATIVE_ALL_SHAPES = "xcomp_progressive|direct_object|intransitive"
_BOOTSTRAP_CESSATIVE_VERB_SHAPES: dict[str, str] = {
    "cease":        _CESSATIVE_ALL_SHAPES,
    "quit":         _CESSATIVE_ALL_SHAPES,
    "discontinue":  _CESSATIVE_ALL_SHAPES,
    "disband":      _CESSATIVE_ALL_SHAPES,
    "abandon":      _CESSATIVE_ALL_SHAPES,
    "halt":         _CESSATIVE_ALL_SHAPES,
    "end":          _CESSATIVE_ALL_SHAPES,
    "fold":         "xcomp_progressive|intransitive",
    "retire":       "xcomp_progressive|intransitive",
    "stop":         "xcomp_progressive",
    "drop":         "xcomp_progressive",
}
_BOOTSTRAP_CESSATIVE_VERBS: frozenset[str] = frozenset(_BOOTSTRAP_CESSATIVE_VERB_SHAPES)

# ── BOOTSTRAP IMPLICATIVE control verb set — DB-DOWN SAFETY NET ONLY ─────────────────
# The bounded LEXICAL class of IMPLICATIVE verbs: a matrix verb whose truth ENTAILS the truth of
# its infinitival complement. "I MANAGED to fix it" entails I fixed it; "I HAD to take it in"
# entails I took it in. This is Karttunen's implicative/non-implicative split (Karttunen 1971,
# "Implicative Verbs", Language 47:340-358) — a lexical-semantic primitive, exactly the kind of
# bounded closed class the cue-rail exists for, NOT a domain word list.
#
# DELIBERATELY EXCLUDED — the irrealis firewall, and the whole reason this set is narrow:
#   * want / plan / hope / intend / decide  — NON-implicative. "I planned to buy it" does NOT
#     entail buying it. These stay in _CATENATIVE/_MENTAL_STATE and must never descend, or the
#     engine starts asserting things the user only contemplated ("user is truth" violated).
#   * fail / forget / neglect              — NEGATIVE implicatives: they entail the NEGATION of
#     the complement. Descending on them would assert the OPPOSITE of what was said. They need
#     polarity handling, not descent, so they are out until that lane exists.
_BOOTSTRAP_IMPLICATIVE_VERBS: frozenset[str] = frozenset({
    "have", "manage", "get", "remember", "bother", "dare", "happen",
})

# ── BOOTSTRAP EXEMPLIFICATION-MARKER set — DB-DOWN SAFETY NET ONLY ──────────────────
# The bounded class of LEXICO-SYNTACTIC EXEMPLIFICATION markers: the surface cues that announce a
# HYPONYM of the preceding general NP — "workshops, LIKE the workshop on X", "pets, SUCH AS a dog",
# "museums INCLUDING the Louvre", "languages, ESPECIALLY Spanish". GROUNDING: Hearst 1992, "Automatic
# Acquisition of Hyponyms from Large Text Corpora", COLING-92 §2 — these are exactly the canonical
# lexico-syntactic patterns ("NP such as NP", "NP including NP", "NP especially NP") whose entire job
# in English is to assert the hyponymy (``instance_of``) relation. Multi-word markers are stored as
# the SPACE-JOINED lowercase surface ("such as") and matched against the marker token run; the
# single-token members are matched on the lemma. Used by ``linguistics._chain_exemplification``.
# The marker alone is NOT sufficient — the chain requires a preceding general NP and a following
# nominal exemplar, so a non-exemplifying "like" ("I felt LIKE a fraud", where "like" is a copular
# complement preposition with no general-NP antecedent) never fires.
# DB-HELD + per-tenant + GROWABLE on the SAME rail (category='exemplification_marker'); this in-code
# set is the DB-DOWN code-fallback seed only, NOT the authority.
#
# The rows ALSO carry a KEYED MODE in `description` (resolve_exemplification_marker_modes):
#   'unambiguous'    — the marker has no non-exemplifying reading in this dep shape ("such as",
#                      "including", "especially", "notably", "particularly").
#   'comma_required' — the marker is POLYSEMOUS and only reads as exemplification when set off by a
#                      comma (nonrestrictive apposition). "like" is the case: "workshops and lectures,
#                      LIKE the workshop on X" exemplifies, but "I treat my dogs LIKE children" /
#                      "I ate lunch LIKE a king" is a MANNER/similarity adjunct and must mint nothing.
# The mode is DATA (same rail, same rows) rather than an in-code per-marker branch — exactly how
# identifier_noun carries its 'strong'/'suffix' role in `description`. Absent/blank → 'unambiguous'.
# ⚠️ SEEDED = the markers that actually surface as a ``prep`` governing a ``pobj`` in the spaCy parse,
# which is the dep shape the chain reads. Hearst's ADVERBIAL markers ("especially", "notably",
# "particularly", "e.g.", "for example") parse as ``advmod`` / a separate ``for``-PP and would NEVER
# match, so seeding them would advertise coverage that does not exist. They are deliberately absent
# until that shape is supported — the growth rail can add them once it is.
_BOOTSTRAP_EXEMPLIFICATION_MARKERS: frozenset[str] = frozenset({
    "such as", "including", "like",
})
_BOOTSTRAP_EXEMPLIFICATION_MARKER_MODES: dict[str, str] = {
    "such as": "unambiguous",
    "including": "unambiguous",
    "like": "comma_required",
}

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

# ── BOOTSTRAP STATIVE-POSSESSION verb set — DB-DOWN SAFETY NET ONLY ──────────────────
# The bounded LEXICAL class of STATIVE possession verbs: a verb whose lexical semantics is the subject
# CURRENTLY POSSESSING its direct object ("I HAVE a dog", "I OWN a motorcycle", "I POSSESS a painting",
# "I KEEP a hamster", "I HOLD a property"). Used by the named-instance self-possession gate
# (`linguistics._type_is_self_possessed`, clause (b)) to decide whether a named instance's TYPE belongs
# to the speaker before the possession edge ((user, owns/has_pet, <name>)) is minted. This is the
# STATIVE (currently-possessing) counterpart of the ACQUISITION (coming-to-possess — got/bought/…)
# class above — a DISTINCT lexical-semantic class: the self-possession gate is about a STANDING
# possession relation, not a transfer event. ⚠️ FLAGGED BOUNDED CLASS (the self-possession-verb-gate
# brief): the possession signal cannot be made purely structural — "I have a dog" (stative possession)
# and "I have a meeting" (light-verb occurrence) share the SAME verb→dobj dep shape; only the verb's
# lexical semantics distinguishes a possession reading. So a small bounded verb class is unavoidable
# here, EXACTLY as for the naming / LVC / inchoative / aspectual / acquisition classes. It is
# firewalled downstream by the parse the SAME way: the gate climbs only to a 1st-person-personal-
# pronoun-subject governing verb, and the named-instance binding already requires a ProperName↔Type
# binding under that verb. ``have`` is INCLUDED so the existing family/pet self-possession path keeps
# working — now AS METADATA, not as the retired in-code ``== "have"`` literal. DB-HELD + per-tenant +
# GROWABLE on the SAME rail (category='possession_verb'); this in-code set is the DB-DOWN code-fallback
# seed only, NOT the authority. Mirrors migration 118's public seed.
_BOOTSTRAP_POSSESSION_VERBS: frozenset[str] = frozenset({
    "have", "own", "possess", "keep", "hold",
})

# ── BOOTSTRAP EMPLOYMENT / ROLE-PREDICATION verb set — DB-DOWN SAFETY NET ONLY ───────
# The bounded LEXICAL class of EMPLOYMENT / ROLE-PREDICATION verbs: a verb whose lexical semantics is
# the subject HOLDING / DISCHARGING a role or affiliation ("I WORK as a nurse at the clinic", "she
# SERVES as treasurer", "he ACTS as mediator", "I am EMPLOYED as an engineer at Globex", "she was HIRED
# as a manager", "he was APPOINTED as chair"). Used by the employment deriver chain
# (``linguistics.derive_sentence_facts`` → ``_chain_employment``) to recognize the
# "<subject> <employment verb> as <role> [at|for <org>]" construction → occupation(<subject>, <role>)
# + works_for(<subject>, <org>). This is what lets the chain be BROAD (any employment verb we've grown)
# without over-capturing: "I DRESSED as a pirate" / "he is KNOWN as Ace" — ``dress``/``know`` are NOT
# in this class, so those are NEVER read as an occupation. The verb cue class IS the safety gate.
#
# ⚠️ FLAGGED BOUNDED LEXICAL CLASS, honestly documented — like naming/acquisition/possession, the
# employment "as <role>" reading cannot be made purely structural: "work as a nurse" (role) and "act as
# a catalyst" vs "dress as a pirate" (costume) share the SAME prep-``as`` dep shape; only the verb's
# lexical semantics distinguishes an employment/role-holding reading. It is firewalled downstream by
# the parse the SAME way (a grammatical subject — 1st-person-personal-pronoun OR a named 3rd-person
# subject — governing the verb, and the ``as``/``at``/``for`` PP frame), and it is DB-HELD + per-tenant
# + GROWABLE (category='employment_verb') so a tenant grows its own employment verbs freq-gated without
# code edits. This in-code set is the DB-DOWN code-fallback seed only, NOT the authority. Mirrors
# migration 125's public seed.
_BOOTSTRAP_EMPLOYMENT_VERBS: frozenset[str] = frozenset({
    "work", "serve", "act", "function", "employ", "hire", "appoint", "contract",
})

# ── BOOTSTRAP RELOCATION / CHANGE-OF-RESIDENCE verb set — DB-DOWN SAFETY NET ONLY ────
# The bounded LEXICAL class of RELOCATION verbs: a verb whose lexical semantics is the subject
# CHANGING RESIDENCE to a destination ("I MOVED to Tokyo", "she RELOCATED to Berlin", "we RESETTLED
# in Halifax", "he EMIGRATED to Canada"). Used by ``linguistics.derive_sentence_facts`` →
# ``_chain_relocation`` to recognize the "<person> <relocation verb> to <place>" construction and emit
# the SAME residence rel the present-tense "live in <place>" path produces — ``lives_in(<subject>,
# <place>)`` — as a state CHANGE (the new current residence). Without this class "I moved to Tokyo"
# folds a NOVEL ``move_to`` predicate that carries no residence semantics and is dropped, so the
# residence linkage is LOST though it is exactly the marquee state-change ("moved cities") the temporal
# model is built for.
#
# ⚠️ FLAGGED BOUNDED LEXICAL CLASS, honestly documented — like naming/acquisition/possession/employment,
# the relocation reading cannot be made purely structural: "move to Tokyo" (change residence) and "move
# to the next item" / "move the box to the shelf" share the verb+``to`` dep shape; only the verb's
# lexical semantics + a PERSON subject + a PLACE destination distinguishes a residence relocation. The
# verb cue class IS the safety gate. It is firewalled downstream by the parse the SAME way (a PERSON
# subject — 1st-person-personal-pronoun OR a PROPN name — and a "to"/"into" destination PP whose pobj is
# a GLiNER2 Location or a PROPN place; a common-noun/abstract destination never fires), and it is
# DB-HELD + per-tenant + GROWABLE (category='relocation_verb') so a tenant grows its own relocation
# verbs freq-gated without code edits. This in-code set is the DB-DOWN code-fallback seed only, NOT the
# authority. Mirrors migration 167's public seed.
_BOOTSTRAP_RELOCATION_VERBS: frozenset[str] = frozenset({
    "move", "relocate", "resettle", "emigrate", "immigrate", "migrate",
})

# ── PROBLEM-NOUN (bland eventive head) class — DB-DOWN / COLD-TENANT FLOOR + grown per-tenant ──
# problem_noun is the eventive-head class of an LVC device-issue: a light verb ("have"/"take"/"get")
# governs a SEMANTICALLY-EMPTY problem-noun dobj whose meaning lives in its ``with``-PP complement
# ("I had an ISSUE with my car's GPS system"). The class GROWS per-tenant from the observed
# construction (re_embedder freq-gate ≥3 → ``<tenant>.linguistic_cues`` category='problem_noun'), so a
# domain's own problem vocabulary accretes without code edits.
#
# It ALSO carries a DB-DOWN / COLD-TENANT BOOTSTRAP FLOOR — a small CLOSED class of generic
# abnormal-state nouns — EXACTLY like every other cue class here (svo_particle, discourse_marker, …).
# WHY a floor (this reverses the earlier empty-set carve, which was the bug): the LVC→has_state bind is
# gated on problem-noun MEMBERSHIP of the dobj (so "had a MEETING/LUNCH/CONVERSATION with X" — eventive
# but NOT a problem — never binds). With an EMPTY floor a fresh/oracle tenant (the harness wipes per Q)
# resolves the class empty → the gate can NEVER fire → "I had an issue with my GPS" FRAGMENTS (the
# device-issue is lost to a bare owns/participated_in). These are GENERIC GRAMMAR-LEVEL problem nouns
# (a closed abnormal-state class), NOT a domain word zoo (no gps/car/device surfaces) — the same
# justification the discourse-marker / particle floors carry. Per-tenant growth still extends it; this
# is only the never-empty fail-safe the resolver docstring already promises.
_BOOTSTRAP_PROBLEM_NOUNS: frozenset[str] = frozenset({
    "issue", "problem", "trouble", "bug", "glitch", "error", "fault",
    "defect", "malfunction", "failure", "difficulty", "complication",
})

# ── BOOTSTRAP POSITION-NOUN set — DB-DOWN / COLD-TENANT FLOOR + grown per-tenant ──────
# position_noun is the class of GENERIC POSITION / APPOINTMENT CONTAINER nouns: a semantically-light
# noun that stands FOR a job/role a person holds and takes the occupation itself in an apposed "as
# <NP>" complement — "my previous ROLE as a marketing specialist", "her new POSITION as CTO", "his
# JOB as a nurse at the clinic". The occupation lives in the "as <NP>" appositive, NOT in the container
# noun; the container noun is the grammatical trigger that a role/occupation is being predicated of its
# POSSESSOR. This is the noun-headed twin of the ``employment_verb`` construction ("I work as a nurse")
# — same "as <role> [at|for <org>]" frame, but triggered by a possessed position noun instead of an
# employment verb (the sentence's main verb is unrelated: "I've USED Trello in my role as …").
#
# It is the SAFETY GATE (exactly like ``employment_verb`` / ``problem_noun``): a possessed noun NOT in
# this class ("my HOUSE as collateral", "the same COLOR as my car") never mints an occupation. These
# are GENERIC GRAMMAR-LEVEL position container nouns (a small closed class), NOT a domain word zoo (no
# occupation titles surface here — those are captured TYPE-agnostically from the "as" complement). It
# is DB-HELD + per-tenant + GROWABLE on the SAME rail as the other cue classes; this in-code set is the
# DB-DOWN code-fallback / cold-tenant floor ONLY. Per-tenant growth still extends it.
_BOOTSTRAP_POSITION_NOUNS: frozenset[str] = frozenset({
    "role", "position", "job", "title", "post", "capacity", "appointment",
    "gig", "stint", "tenure", "function",
})

# ── BOOTSTRAP SHELL-NOUN set — DB-DOWN / COLD-TENANT FLOOR + grown per-tenant ─────────
# shell_noun is the class of GENERIC ABSTRACT/SHELL nouns — semantically-light anaphoric heads that a
# later sentence uses to REFER BACK to a previously-introduced entity ("The FLAW has been exploited",
# "The RULING overruled Baker", "The CONDITION worsened"). These are NOT domain terms: they are the
# domain-agnostic shell-noun inventory of English discourse (Schmid's "shell nouns" / Halliday's
# general nouns) that recurs across EVERY subject (a CVE, a court case, a diagnosis, a device fault all
# get called "the issue"/"the matter"/"the thing"). The cross-sentence discourse-topic coref
# (derive_sentence_facts._topic_definite_subject) consults this set: a DEFINITE subject NP whose head
# is in this class, with no closer antecedent, binds to the turn's topic — so a later description that
# uses a generic shell co-referent (which GLiNER2 does NOT coarse-match to the topic's exact type
# noun — "flaw" ≉ "vulnerability") still CONSOLIDATES onto the topic instead of islanding.
#
# It is DB-HELD + per-tenant + GROWABLE on the SAME rail as the other cue classes; this in-code set is
# the DB-DOWN code-fallback / cold-tenant floor ONLY (never the authority). The bind is heavily gated
# by the parse (ONE unambiguous topic, DEFINITE determiner — an INDEFINITE "a flaw" introduces a NEW
# entity and is never bound, no closer in-sentence antecedent), so an over-broad floor cannot
# over-bind. These are generic grammar-level abstract nouns, NOT a domain word zoo.
_BOOTSTRAP_SHELL_NOUNS: frozenset[str] = frozenset({
    # generic abstract/shell heads (subject-agnostic — recur across every domain)
    "flaw", "issue", "problem", "matter", "condition", "situation", "case",
    "finding", "defect", "fault", "entity", "item", "thing",
    # domain-neutral "outcome/act" shells that commonly re-refer (a ruling, a decision, an incident)
    "ruling", "decision", "incident",
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
# ATTRIBUTE-NOUN detection class — INTENTIONALLY EMPTY (see ATTRIBUTE_NOUN_CATEGORY below for the
# full rationale). Every member of this class is domain vocabulary, so there is no defensible in-code
# seed; the class starts empty per tenant and GROWS from observed constructions via the carved-cue
# candidate queue. An empty frozenset is the correct fail-safe for a DETECTION class: nothing is
# admitted by shape alone, so a miss CONTAINS the construction instead of capturing a guess.
_BOOTSTRAP_ATTRIBUTE_NOUNS: frozenset[str] = frozenset()

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
    "son", "daughter", "child", "kid", "wife", "husband", "spouse", "partner",
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
    "son": "child_of", "daughter": "child_of", "child": "child_of", "kid": "child_of",
    "wife": "spouse", "husband": "spouse", "spouse": "spouse", "partner": "spouse",
    "uncle": "related_to", "aunt": "related_to", "cousin": "related_to",
    "grandmother": "related_to", "grandfather": "related_to",
    "grandma": "related_to", "grandpa": "related_to",
}

# ── BOOTSTRAP KINSHIP-NOUN → GENDER MAP — DB-DOWN SAFETY NET ONLY ────────────────────
# A MAP (kinship noun lemma → the gender the role intrinsically carries) for the named-instance
# binding chain: "a son Alex" → son is intrinsically MALE → (alex, has_gender, male); "a daughter
# Robin" → female. This is the SAME (cue, category) rail as the kinship_noun → rel_type map, in a
# DISTINCT category ('kinship_gender') so one row class carries the rel and another carries the gender
# (a single noun can be in both — the binding chain consults each map independently). ONLY the
# gendered kin roles appear; a GENDER-NEUTRAL kin role (child / parent / sibling / spouse / partner /
# cousin) is INTENTIONALLY ABSENT so no gender is fabricated where the language does not state one. The
# value is a STRING gender token routed to the SCALAR ``has_gender`` rel (tail_types={SCALAR}). Stored
# on the SAME rail: `cue` = the kinship noun, `description` = the gender. DB-DOWN code-fallback seed
# only — mirrors migration 117's public seed. A noun OUTSIDE this map → no gender minted (never guessed).
_BOOTSTRAP_KINSHIP_GENDER_MAP: dict[str, str] = {
    "son": "male", "daughter": "female",
    "mother": "female", "father": "male", "mom": "female", "dad": "male",
    "sister": "female", "brother": "male",
    "wife": "female", "husband": "male",
    "uncle": "male", "aunt": "female",
    "grandmother": "female", "grandfather": "male",
    "grandma": "female", "grandpa": "male",
}

# ── SOCIAL-ROLE-NOUN → REL_TYPE MAP — universal tie SEEDED, domain roles GROWN ──────
# CARVE-OUT (lean-seed): the DOMAIN-FLAVORED social roles (boss/colleague/roommate/classmate/coworker/
# neighbour/acquaintance/manager) vary by domain and are NOT grammar primitives — so they are NOT
# seeded. They are GROWN PER-TENANT from the OBSERVED construction: a possessed/apposed COMMON-noun role
# head governing a PERSON-typed named instance ("my colleague Sam", "a roommate named Dana") that is
# NEITHER kinship NOR an already-grown social role → the role noun is queued (``linguistic_cue_candidate``
# → re_embedder freq-gate ≥3 → ``<tenant>.linguistic_cues`` category='social_role', grown rel_type = the
# generic person tie ``knows``). On a COLD tenant such an unknown role DEGRADES to the generic walkable
# ``related_to(name, user)`` (a PERSON is never ``owns``) and queues the role — NEVER dropped/errored.
#
# BUT ``friend`` is the ONE UNIVERSAL, subject-agnostic social primitive (parity with the seeded kinship
# class — mother/son/… are seeded because they are universal, not domain-flavored). Migration 123's
# carve-out over-removed ``friend`` along with the domain roles, which regressed the social-role COPULA
# ("my friend is Sam" fell through to has_role + ``owns(user, sam)`` — a PERSON owned, the very invariant
# the carve-out promised to hold). We restore ONLY the universal tie here: ``friend → friend_of``. This
# floor is what the DSN-unset / carved-tenant path resolves (``_resolve_keyed_map`` returns the bootstrap
# when the tenant's social_role rows are empty), so "my friend is Sam" → friend_of(sam, user), collapsing
# the role noun so ``friend`` is never a standalone owned entity. Domain roles stay GROWN (empty here).
# ⚠️ WIDENED 2026-08-27 to MIRROR migration 272 row-for-row (46 members). The single-member floor
# above was the residue of migration 123's carve-out, which is now overruled: social_role is
# CLOSED-CLASS English structure (colleague/coworker/teammate/roommate), not domain flavour, and per
# the owner's ruling engine scaffolding is seeded — it does not wait on confirmations. The inventory
# is WORDNET-DERIVED, not hand-written: regenerate with
#     python3 the internal design record
# and diff both this literal AND migration 272's VALUES against its output. Like every other floor in
# this module it is the DB-DOWN code-fallback mirror, NEVER the authority, and it must never be more
# permissive than migration 272.
_BOOTSTRAP_SOCIAL_ROLE_MAP: dict[str, str] = {
    # THE UNIVERSAL SOCIAL PRIMITIVE ONLY. `friend` is a grammar-level tie, not domain
    # vocabulary, and it is the never-empty fail-safe for an unbound/unreadable tenant.
    #
    # ⚠️ DO NOT RE-ADD A ROLE LEXICON HERE. Migration 272 seeds 45 WordNet-derived
    # social_role rows into `public.linguistic_cues`, which provisioning fans into every
    # tenant — that DB rail IS the carrier, and it grows per-tenant. Duplicating those
    # words in code enumerates domain vocabulary in the engine, which this project
    # forbids outright (subject-agnostic & growable: miss -> GROW, never hardcode), and
    # it forks the truth: a tenant correction (`SET is_active = false`) cannot suppress
    # a cue an in-code floor keeps re-asserting.
    #
    # This is pinned by tests/test_linguistics.py::test_carved_class_bootstraps_are_empty
    # and ::test_carved_consumers_resolve_empty_when_unbound. If either goes red, a word
    # zoo was re-added here — move it to a migration instead.
    "friend": "friend_of",
}

# ── ROLE-NOUN → REL_TYPE MAP (copula predicate-nominal role chain) — DB-DOWN SAFETY NET ──
# A MAP (first-person-possessed role noun → rel_type) for the predicate-nominal role chain:
# "Globex Industries is my employer" → employer → works_for → (user, works_for, globex industries).
# CONVENTION — DISTINCT from the kinship/social_role maps (which run FILLER→user, e.g. mother→
# parent_of ⇒ (mother, parent_of, user)): here the value is the rel_type FROM the POSSESSOR (the
# user) TO the FILLER entity (the copula SUBJECT NP). Do NOT merge the two conventions into one
# category — that is a direction bug waiting to happen. Seeded SMALL (the universal employment
# primitives, all landing on the seeded ``works_for`` rel, P108, tail_types={Organization,Person});
# growth adds domain roles per-tenant. ``employee`` is INTENTIONALLY ABSENT: works_for has NO
# seeded inverse rel_type (inverse_rel_type=NULL), so "<Name> is my employee" has no honest
# user→filler rel to map — we no-op rather than fabricate. Stored on the SAME (cue, category)
# rail: `cue` = the role noun lemma, `description` = the rel_type. DB-DOWN code-fallback seed
# only — mirrors migration 142's public seed.
_BOOTSTRAP_ROLE_NOUN_MAP: dict[str, str] = {
    "employer": "works_for",
    "boss": "works_for",
    "manager": "works_for",
    "supervisor": "works_for",
}

# ── ALIAS-PREDICATE (verb → licensing PP particle) MAP — DB-DOWN SAFETY NET ONLY ─────
# The bounded GRAMMATICAL class of PHRASAL alias/naming predicates: a verb whose lexical semantics,
# TOGETHER WITH a specific licensing preposition, predicates that a subject is KNOWN BY / GOES BY a
# name ("she GOES BY Dee", "he is KNOWN AS Sammy", "she is REFERRED TO AS Liv"). The MAP value is the
# licensing PARTICLE the verb must govern for the alias reading (go→"by", know→"as", refer→"as") — it
# is the disambiguator that separates the alias reading from a same-verb NON-naming use ("she GOES to
# work" — "go" without a "by"-PP naming a proper noun is motion, not an alias). This is DISTINCT from
# the ``naming_verb`` SET (call/name/title/dub/…), which are single naming VERBS that take the name as
# a direct complement ("prefers to be CALLED Liv") and need no licensing particle.
#
# ⚠️ FLAGGED BOUNDED LEXICAL/PHRASAL CLASS, honestly documented — like naming/kinship, the phrasal
# alias reading cannot be made purely structural: "go by X" (alias) and "go by the store" (motion via)
# share the verb+``by`` dep shape; only the verb's phrasal semantics + a PROPER-NOUN pobj distinguishes
# them. It is firewalled downstream by the parse the SAME way (a licensing particle whose ``pobj`` is a
# PROPN name, subject resolved by grammatical person/coref), and it is DB-HELD + per-tenant + GROWABLE
# (category='alias_predicate') so a tenant grows its own alias idioms freq-gated without code edits.
# Mirrors the codebase's existing go-by nickname idiom (linguistics._nickname_run). This in-code map
# is the DB-DOWN code-fallback seed only, NOT the authority. Mirrors migration 146's public seed.
_BOOTSTRAP_ALIAS_PREDICATE_MAP: dict[str, str] = {
    "go": "by",
    "know": "as",
    "refer": "as",
}

# ── BOOTSTRAP MEASUREMENT-UNIT → SCALAR REL_TYPE MAP — DB-DOWN SAFETY NET ONLY ───────
# A MAP (measurement-unit head lemma → the SCALAR rel_type it measures) for the copula measurement
# chain: "she is 62 years old" → unit "year" → age; "he is 6 feet tall" → unit "foot" → height; "it
# weighs 80 kilograms" → unit "kilogram" → weight. These rel_types carry tail_types={SCALAR} so the
# value routes to entity_attributes. The bare-age fallback ("Robin is 28" — a NUM attr with no unit)
# resolves to `age` via the deriver's grammatical age-shape, NOT this map. A unit OUTSIDE this map →
# no scalar minted (we never guess a measurement). Stored on the SAME (cue, category) rail: `cue` =
# the unit lemma, `description` = the scalar rel_type. DB-DOWN code-fallback seed only.
_BOOTSTRAP_UNIT_SCALAR_MAP: dict[str, str] = {
    "year": "age",
    "foot": "height", "feet": "height", "inch": "height",
    "centimetre": "height", "centimeter": "height", "cm": "height", "metre": "height", "meter": "height",
    "pound": "weight", "lb": "weight", "kilogram": "weight", "kg": "weight", "kilo": "weight",
    # TIME units → duration. These are MEASUREMENT PRIMITIVES (a bounded unit lexicon on the same
    # rail as foot/pound), NOT domain literals: the map only says "a NUM-quantified <time-unit> is a
    # measured DURATION", which is what lets the possessed-measure detector recognize "my commute
    # takes 45 minutes" as a scalar (like "my address is …") rather than an island graph edge.
    "second": "duration", "minute": "duration", "hour": "duration",
    "day": "duration", "week": "duration", "month": "duration",
    # MASS/VOLUME units → quantity (dosage-frame, issue #14). Same bounded measurement-primitive
    # lexicon rule as the rows above: the map only says "a NUM-quantified <mass/volume unit> is a
    # measured QUANTITY" — the generic quantity attribute the quantity-of chain already falls back
    # to, so ingest output is unchanged for these units; what the rows ADD is family
    # RECOGNITION (the measure/dosage-interrogative admission's value-unit test can admit a
    # stored "10 milligrams" scalar as its own family instead of deferring to fetch-all).
    # Closed under the same rule as foot/pound: only units whose measurement sense is a lexical
    # fact join. Abbreviations ride the same entries (mg/g/ml), never a drug or domain word.
    "gram": "quantity", "grams": "quantity", "g": "quantity",
    "milligram": "quantity", "milligrams": "quantity", "mg": "quantity",
    "microgram": "quantity", "micrograms": "quantity", "mcg": "quantity", "ug": "quantity",
    "litre": "quantity", "litres": "quantity", "liter": "quantity", "liters": "quantity",
    "l": "quantity", "millilitre": "quantity", "millilitres": "quantity",
    "milliliter": "quantity", "milliliters": "quantity", "ml": "quantity",
    "unit": "quantity", "units": "quantity",
}

# ── BOOTSTRAP MEASUREMENT-VERB SET — DB-DOWN SAFETY NET ONLY ────────────────────────────────────────
# The measurement-VERB lemma set for the VERB-MEASURE scalar lane's MIS-TAG arm (linguistics
# ``VERB_MEASURE_SCALAR`` V2): a ROOT token spaCy tagged NOUN ("measures" → NNS) recovers the
# measurement reading only if its LEMMA is a lexical measurement verb (Quirk et al., CGEL ch. 9 —
# measure/weigh/span take a measure-phrase complement as their core frame). Deliberately SMALL and
# closed under the same rule as the sibling floors: only verbs whose measurement sense is a
# LEXICAL fact join (a polysemous verb — run/cost/last — is admitted by TENANT GROWTH, never by
# widening this floor, because a wrong floor fires on the wrong lemmas). DB-DOWN code-fallback
# seed only; the live authority is the per-tenant ``measure_verb`` cue class.
_BOOTSTRAP_MEASURE_VERBS: frozenset[str] = frozenset({"measure", "weigh", "span"})

# ── THIN-TYPE MAP — CARVED (NOT seeded; degrade is LOSSLESS, active growth DEFERRED) ──
# CARVE-OUT (lean-seed): thin_type is a DOMAIN-FLAVORED device/system synonym MAP (device/gadget/
# appliance/machine → device), NOT a grammar/unit primitive — so it is NO LONGER SEEDED and the
# DB-DOWN code-fallback is now EMPTY. thin_type is ONLY a coarse slot-type FALLBACK that GLiNER2's
# live typing already WINS over, so an empty map is LOSSLESS: a cold tenant simply uses GLiNER2's
# type (or the generic Object), nothing is dropped. ACTIVE GROWTH IS DEFERRED (honest residual): the
# only candidate signal — "GLiNER2 typed this head as X" — is circular (GLiNER2 already supplies that
# type live), so a freq-gated grow would just re-cache what GLiNER2 says with no new capability. The
# carve removes the seeded device vocabulary (the brief's core ask); growth is left as a no-op until a
# non-circular signal exists.
_BOOTSTRAP_THIN_TYPE_MAP: dict[str, str] = {}

# ── IDENTIFIER-NOUN class — DB-HELD + per-tenant + GROWABLE (category='identifier_noun') ──────────
# The CONTEXT-SIGNAL class for a stated reference/identifier code ("my ticket number is 1234567",
# "the docket number is 2024-CV-00931"): the HEAD/compound noun of the copula subject NP signals that
# the post-copula value is an IDENTIFIER, so it is captured (has_reference_id) REGARDLESS of value
# shape — including a BARE NUMBER that the value-shape atomic pattern (migration 185) intentionally
# excludes to avoid eating counts. The context noun is what disambiguates: "1234567" after "ticket
# number is" is an ID, not a count.
#
# ROLE ('description' column, resolved by resolve_identifier_noun_roles → {noun: role}):
#   • 'strong' — INHERENTLY identifier-signalling; establishes the context ALONE ("my case is X",
#     "my id is X", "my reference is X") or as a COMPOUND of a generic head ("ticket number").
#   • 'suffix' — the AMBIGUOUS generic tail ("number"/"id"/"code"): identifier-SHAPED but NOT
#     sufficient alone (a bare "number" is "favorite number"/"phone number"/a count). It only rides
#     ALONGSIDE a 'strong' cue; the deriver gate requires a 'strong' cue present, so "my favorite
#     number is 7" (no strong cue) NEVER fires. Note "id"/"code" are 'strong' (they mean identifier
#     unambiguously); only "number" is a 'suffix'.
# Grown per-tenant on the SAME rail; this in-code set is the DB-DOWN code-fallback seed only (mirrors
# migration 186's public seed).
_BOOTSTRAP_IDENTIFIER_NOUN_ROLE_MAP: dict[str, str] = {
    # strong — establish identifier context alone or as a compound
    "ticket": "strong", "case": "strong", "docket": "strong", "order": "strong",
    "account": "strong", "policy": "strong", "claim": "strong", "reference": "strong",
    "confirmation": "strong", "invoice": "strong", "id": "strong", "code": "strong",
    # suffix — generic tail; rides alongside a strong cue, never triggers alone
    "number": "suffix",
}
_BOOTSTRAP_IDENTIFIER_NOUNS: frozenset[str] = frozenset(_BOOTSTRAP_IDENTIFIER_NOUN_ROLE_MAP)

# ── PHASE LABELS for the aspectual-control rail — DB-DOWN SAFETY NET ONLY ────────────
# NEGATION DOES NOT SCOPE UNIFORMLY OVER THE PHASE RAIL, and treating it as one class makes the
# engine store the semantic opposite of what the person said:
#   * INGRESSIVE / CONTINUATIVE — negating the matrix negates the complement.
#     "I did not continue brewing" -> the brewing is not ongoing.
#   * TERMINATIVE               — negating the matrix AFFIRMS the complement.
#     "I did not stop drinking coffee" -> the person STILL DRINKS COFFEE. Only the
#     presupposition projects (they used to); the assertion does not.
# The phase is ALREADY carried per-row in `description` by migration 113 ("Ingressive phase verb
# …" / "Continuative phase verb …" / "Terminative phase verb …") — the same set+label-on-one-rail
# shape kinship_noun and cessative_verb use. This map is ONLY the DB-DOWN mirror of those labels
# and never the authority. It mirrors migration 113 row-for-row; verify against that file, not from
# memory, and never let it become more permissive than the seed.
# ⚠️ The class is per-tenant GROWABLE, so a GROWN terminative would silently inherit the wrong
# scoping if the consumer guessed. The consumer therefore admits ONLY phases it positively
# recognises as scoping down; an unlabelled or unknown grown row falls back to NOT marking.
_BOOTSTRAP_ASPECTUAL_CONTROL_PHASES: dict[str, str] = {
    "start":    "Ingressive phase verb raising the subject over a progressive -ing activity",
    "begin":    "Ingressive phase verb raising the subject over a progressive -ing activity",
    "continue": "Continuative phase verb raising the subject over a progressive -ing activity",
    "keep":     "Continuative phase verb raising the subject over a progressive -ing activity",
    "resume":   "Continuative phase verb raising the subject over a progressive -ing activity",
    "commence": "Ingressive phase verb (formal) raising the subject over a progressive -ing activity",
    "finish":   "Terminative phase verb raising the subject over a progressive -ing activity",
    "stop":     "Terminative phase verb raising the subject over a progressive -ing activity",
}

# ── POLARITY LABELS for the implicative rail — DB-DOWN SAFETY NET ONLY ───────────────
# The same asymmetry, one rail over (Karttunen 1971). A POSITIVE implicative entails its
# complement, so negating the matrix negates the complement ("I did not manage to fix it" -> not
# fixed). A NEGATIVE implicative (fail / forget / neglect) entails the complement's NEGATION, so
# negating the matrix AFFIRMS it ("I did not fail to fix it" -> fixed). The seeded rows carry
# "Positive implicative (Karttunen 1971)" in `description`; the negative ones are excluded from the
# seed BY COMMENT ONLY, with no mechanism — and the class is growable, so a grown negative
# implicative would invert exactly like a grown terminative. The consumer reads this label and
# admits only rows positively identified as positive.
# ⚠️ MIRRORS THE SEED TEXT ROW-FOR-ROW — it must NEVER be more permissive than the data it stands
# in for. A blanket "Positive implicative" for every member was, and it made behaviour differ BY
# ENVIRONMENT in the forbidden direction: `_resolve_keyed_map` REPLACES this floor with tenant rows
# rather than merging, so DB-up read migration 196 and correctly declined to mark `have` (labelled a
# modal-necessity matrix) and `manage` (labelled without the positive term), while DB-down marked
# both. Any edit here must be diffed against migration 196, not written from memory.
_BOOTSTRAP_IMPLICATIVE_POLARITIES: dict[str, str] = {
    "get":      'Positive implicative (Karttunen 1971): "got to X" entails X happened',
    "remember": 'Positive implicative (Karttunen 1971): "remembered to X" entails X happened',
    "bother":   "Positive implicative (Karttunen 1971)",
    "dare":     "Positive implicative (Karttunen 1971)",
    "happen":   "Positive implicative (Karttunen 1971); currently blocked by the _CATENATIVE firewall",
    # NOT labelled positive in the seed -> the consumer declines to mark. Kept verbatim so the
    # floor and the seed agree, and so the divergence cannot silently reappear.
    "have":     "Modal-necessity matrix; PAST + perfective forces an actuality entailment "
                "(Bhatt 1999 / Hacquard 2006)",
    "manage":   "Karttunen paradigm implicative; currently blocked by the _CATENATIVE firewall "
                "(documented under-capture)",
}




# ── BOOTSTRAP CONTINUATIVE / CANCELLING COMPARATIVE ADVERBS — DB-DOWN SAFETY NET ONLY ─
# THE CLOSED SIDE OF A TWO-SIDED DIVISION, and seeding THIS side rather than its complement is the
# architectural correction that ended five rounds of guard failures:
#   * CONTINUATIVE / CANCELLING — "no longer", "no more". The negator cancels the EVENT.
#     A CLOSED, two-member set.
#   * COMPARATIVE-SCOPE — every OTHER negated comparative ("no earlier", "no harder", "no faster",
#     "no fewer", "no higher", "no cheaper", "no farther", "no smaller", …). The negator scopes over
#     the COMPARISON; the event is ASSERTED. An OPEN, PRODUCTIVE class — Wiktionary's adverb sense
#     of `no` glosses the frame generically ("before comparatives with more and less, and
#     idiomatically before other comparatives") with no member list, because there isn't one.
# Every earlier attempt enumerated the OPEN side and each shipped a falsehood; you cannot finish
# enumerating a productive class. Admitting the closed side is complete BY CONSTRUCTION and flips
# the fail-safe: an unknown negated comparative defaults to NOT-a-cancellation (event asserted)
# rather than to a stored denial. MEASURED on a two-sided corpus: old arrangement 28/37, this 37/37,
# with zero cancellations lost.
#
# MEMBERS ARE LEMMAS, measured PER TAG (the lesson from "worse", whose lemma is tag-dependent):
#     surface "longer" -> lemma `long`  (RB and RBR alike)
#     surface "more"   -> lemma `more`  (JJR and RBR alike)
#
# This class SUBSUMES and replaces the former `correlative_adverb` and `emphatic_adverb` classes,
# which were enumerations of the open side; they are deleted rather than left dead.
#
# ⚠️ EMPTY IS THE CATASTROPHIC MODE. Because this class ADMITS rather than suppresses, emptying it
# silently stops every cancellation from registering — the consumer therefore treats an empty
# resolution as a fault, falls back to this floor, and logs loudly.
_BOOTSTRAP_CONTINUATIVE_ADVERBS: frozenset[str] = frozenset({"long", "more"})


# Cue CATEGORIES this module resolves. The table is general by category so every verb/particle cue
# class rides the SAME rail (one table, one overlay) without a new module.
NAMING_VERB_CATEGORY = "naming_verb"
NAMING_NOUN_CATEGORY = "naming_noun"
LVC_SUPPORT_VERB_CATEGORY = "lvc_support_verb"
INCHOATIVE_VERB_CATEGORY = "inchoative_verb"
ASPECTUAL_CONTROL_VERB_CATEGORY = "aspectual_control_verb"
# cessative_verb — the CESSATIVE-aspect verb class (SIL Glossary "Cessative Aspect"; Freed 1979
# aspectual verbs). A SET class on the SAME rail whose rows ALSO carry the `|`-joined ADMITTED
# COMPLEMENT SHAPES in `description` (see _BOOTSTRAP_CESSATIVE_VERB_SHAPES). DELIBERATELY NOT
# `aspectual_control_verb` — that class mixes ingressive/continuative/terminative for another job.
CESSATIVE_VERB_CATEGORY = "cessative_verb"
IMPLICATIVE_VERB_CATEGORY = "implicative_verb"
# continuative_adverb — the CLOSED set of negated comparative adverbials that CANCEL their event
# ("no longer"/"no more"). Everything else in that frame is comparative-scope and asserts its event.
# See _BOOTSTRAP_CONTINUATIVE_ADVERBS.
CONTINUATIVE_ADVERB_CATEGORY = "continuative_adverb"
# exemplification_marker — the Hearst (COLING-92) lexico-syntactic hyponymy cues ("such as",
# "including", "like"). A SET class on the SAME rail whose rows ALSO carry a keyed POLYSEMY MODE
# in `description` (see _BOOTSTRAP_EXEMPLIFICATION_MARKERS), exactly like identifier_noun.
EXEMPLIFICATION_MARKER_CATEGORY = "exemplification_marker"
ACQUISITION_VERB_CATEGORY = "acquisition_verb"
POSSESSION_VERB_CATEGORY = "possession_verb"
EMPLOYMENT_VERB_CATEGORY = "employment_verb"
RELOCATION_VERB_CATEGORY = "relocation_verb"
PROBLEM_NOUN_CATEGORY = "problem_noun"
POSITION_NOUN_CATEGORY = "position_noun"
SVO_PARTICLE_CATEGORY = "svo_particle"
RELATIONAL_NOUN_CATEGORY = "relational_noun"
# attribute_noun is a flat SET class (the head noun of a POSSESSED ATTRIBUTE NP — "hatch count",
# "wing span", "serial") on the SAME rail as relational_noun, resolved by resolve_attribute_nouns()
# into a frozenset. It is the DISCRIMINATOR for the one genuinely ambiguous value shape in the
# possessive-attribute copula: a SINGLE-WORD ADJECTIVAL complement ("teal"). Value SHAPE alone cannot
# separate "<possessor>'s <attribute> is <adjectival value>" (a scalar) from the PREFERENCE seam's
# "my favourite colour is blue", so the decision is moved onto the ATTRIBUTE side, where it is a
# membership question the growth rail can answer per tenant.
#
# ⚠️ THE BOOTSTRAP FLOOR IS DELIBERATELY EMPTY. This is a DETECTION class whose members are, by
# construction, DOMAIN vocabulary (a plimwick's "hatch count", a server's "uptime", a fabric's
# "weave"). Enumerating any of it in code would be exactly the subject-assumption the engine forbids,
# and an over-broad floor would silently swallow the preference seam. Empty floor ⇒ on a cold tenant
# the single-word-ADJ shape is NEVER captured by shape alone; it is CONTAINED (no junk entity, no
# value) and PROPOSED on the growth queue. Fail safe = no capture + loud, never a silent success.
ATTRIBUTE_NOUN_CATEGORY = "attribute_noun"
DISCOURSE_MARKER_CATEGORY = "discourse_marker"
KINSHIP_NOUN_CATEGORY = "kinship_noun"
# shell_noun is a flat SET class (generic abstract/shell anaphoric heads) on the SAME rail, resolved by
# resolve_shell_nouns() into a frozenset. Used by the cross-sentence discourse-topic coref to bind a
# definite generic co-referent ("the flaw"/"the ruling"/"the condition") back to the turn's topic.
SHELL_NOUN_CATEGORY = "shell_noun"
# Thin-type is a KEYED-VALUE class (surface→type) on the SAME rail (cue=surface, description=type),
# resolved by resolve_thin_type() into a dict — not by the set-returning resolve_cues path.
THIN_TYPE_CATEGORY = "thin_type"
# The kinship_noun rows ALSO carry a KEYED VALUE (noun→rel_type) in `description`, resolved by
# resolve_kinship_rel_map() into a {noun: rel_type} dict (same rail, same rows as the kinship_noun
# SET — the SET is resolve_kinship_nouns, the MAP is resolve_kinship_rel_map). No separate category.
# unit_scalar is its OWN keyed class (unit-lemma → scalar rel_type) for the copula measurement chain.
UNIT_SCALAR_CATEGORY = "unit_scalar"
# measure_verb is a flat SET class (measurement VERB lemmas) on the SAME rail, resolved by
# resolve_measure_verbs() into a frozenset. Used by the VERB-MEASURE scalar lane's MIS-TAG arm
# (linguistics ``VERB_MEASURE_SCALAR`` V2): spaCy mis-tags "measures" NOUN/NNS and ROOTs the clause
# ("An adult blue-ringed octopus measures about 5 centimeters across."), removing the verb category
# itself, so the cue class licenses the measure READING of that noun — the frame STRUCTURE (compound
# subject + nummod'd unit NP + the shared NER QUANTITY discriminator) still gates. A properly
# tagged verb never consults this class (grammar owns that arm).
MEASURE_VERB_CATEGORY = "measure_verb"
# identifier_noun is BOTH a SET (resolve_identifier_nouns — is this head an identifier-context noun)
# AND a KEYED value (resolve_identifier_noun_roles — {noun: 'strong'|'suffix'} in `description`) on
# the SAME rail/rows, exactly like kinship_noun. Drives the context-signalled has_reference_id capture.
IDENTIFIER_NOUN_CATEGORY = "identifier_noun"
# kinship_gender is a KEYED class (kinship-noun → gender) on the SAME rail (cue=noun, description=
# gender), resolved by resolve_kinship_gender_map() into a {noun: gender} dict. Distinct category from
# kinship_noun so the rel-map and the gender-map ride separate rows for the same noun.
KINSHIP_GENDER_CATEGORY = "kinship_gender"
# social_role is a KEYED class (person-social-role noun → rel_type) on the SAME rail (cue=noun,
# description=rel), resolved by resolve_social_role_map() into a {noun: rel_type} dict.
SOCIAL_ROLE_CATEGORY = "social_role"
# role_noun is a KEYED class (possessed role noun → rel_type, USER→FILLER direction — see
# _BOOTSTRAP_ROLE_NOUN_MAP) on the SAME rail, resolved by resolve_role_noun_map() into a dict.
ROLE_NOUN_CATEGORY = "role_noun"
# alias_predicate is a KEYED class (phrasal alias verb → licensing PP particle) on the SAME rail
# (cue=verb lemma, description=particle), resolved by resolve_alias_predicate_map() into a
# {verb: particle} dict. Used by the third-party nickname/alias deriver chain ("goes by X",
# "known as X"). DISTINCT from naming_verb (single naming verbs taking the name as a direct object).
ALIAS_PREDICATE_CATEGORY = "alias_predicate"

# dosage_noun is BOTH a SET (resolve_dosage_nouns — is this head noun a dosage-FAMILY member) AND
# a KEYED value (resolve_dosage_canonical_map — {noun: canonical attribute} in `description`) on
# the SAME rail/rows, exactly like kinship_noun. Drives the ONE dosage-family resolution across
# the three seams (issue #18): the ingest copula weld rebinds the family noun to the canonical
# attribute on the substance, the correction addressing maps any family-member guess/mint onto the
# STORED family attribute, and the query mirror-frame binds the family interrogative. The seed
# floor is the FIVE family members the owner data call discharged (dose/dosage/quantity/level/
# amount), each canonicalized to `quantity` — the attribute the #14 take-frame already writes.
# Subject-agnostic, per-tenant GROWABLE (a new member joins by tenant growth, never in code).
_BOOTSTRAP_DOSAGE_NOUNS: frozenset[str] = frozenset(
    {"dose", "dosage", "quantity", "level", "amount"})
_BOOTSTRAP_DOSAGE_CANONICAL_MAP: dict[str, str] = {
    "dose": "quantity", "dosage": "quantity", "quantity": "quantity",
    "level": "quantity", "amount": "quantity",
}
DOSAGE_NOUN_CATEGORY = "dosage_noun"

# measure_noun (issue #19 W1) — the measurement-family noun class BEYOND dosage, on the SAME
# dual contract as dosage_noun: a SET (resolve_measure_nouns — is this head noun a measurement-
# family member) AND a KEYED value (resolve_measure_canonical_map — {noun: canonical attribute}
# in `description`). The #18 dosage floor covers {dose,dosage,quantity,level,amount}; the
# deploy-#10 battery measured possessive-quantity copula frames WELDING for every family beyond
# it ('my bench press personal record is 140 kilograms' → the amount annihilated on an owns
# island) because the rebind gated on the dosage class alone. This rail is the GROWTH path the
# owner ruling demands: a family member joins by SEEDED FLOOR or TENANT GROWTH (freq-gated cue
# candidates), never code enumeration. The floor is deliberately TWO members, both measurement
# nouns whose measured sense is a LEXICAL FACT (Quirk et al., CGEL ch.5 §8: measure nouns denote
# quantities on a scale): 'record' (a registered best measurement) and 'vocabulary' (a counted
# lexicon size). Each canonicalizes to ITSELF — a singleton family keeps its own name; the keyed
# map exists so a tenant can unify synonyms ('pr' → record) the same way dosage unifies dose →
# quantity. Subject-agnostic, per-tenant GROWABLE.
_BOOTSTRAP_MEASURE_NOUNS: frozenset[str] = frozenset({"record", "vocabulary"})
_BOOTSTRAP_MEASURE_CANONICAL_MAP: dict[str, str] = {
    "record": "record", "vocabulary": "vocabulary",
}
MEASURE_NOUN_CATEGORY = "measure_noun"

# LOCATIVE-PARTICIPLE class (category='locative_participle'). The set of PAST-PARTICIPLE lemmas that,
# in a copula/passive containment idiom "<X> is <participle> <in|at|on|within|inside> <place>", express
# CONTAINMENT/POSITION → the seeded ``located_in`` hierarchy rel ("Rack-2 IS LOCATED IN row-a",
# "the server IS SITUATED IN dc-toronto", "core-1 IS MOUNTED IN rack-1"). Subject-agnostic: the class
# is the discriminator that keeps the containment reading from firing on a non-locative passive+prep
# ("is WRITTEN in Python", "is MADE in China") whose "in" is NOT a container. A ⚠️ FLAGGED BOUNDED
# LEXICAL CLASS (like naming/relocation) — a locative-passive reading cannot be made purely structural
# from the preposition alone; the participle vocabulary + the containment-prep parse gate is the
# discriminator. DB-HELD + per-tenant + GROWABLE.
_BOOTSTRAP_LOCATIVE_PARTICIPLES: frozenset[str] = frozenset({
    "locate", "situate", "position", "house", "install", "mount", "station", "base", "place", "site",
    "instal",  # spaCy en_core_web_sm lemmatizes "installed" → "instal" (one L) — same class member
})
LOCATIVE_PARTICIPLE_CATEGORY = "locative_participle"

# ── NATAL-PREDICATE class (category='natal_predicate') + OFFSPRING-NOUN class ─────────
# The BIRTH-EVENT (natal) linguistic frame — FrameNet "Being_born"/"Giving_birth". Two DB-held,
# per-tenant, GROWABLE cue classes drive the deriver's ``_chain_natal_birth`` (a newborn NAMED person
# is typed ``instance_of`` the birth type + dated), subject-agnostically (NO in-code verb/name list):
#   • natal_predicate (VERBS): the passive/transitive birth predicates — "was BORN" (spaCy lemmatizes
#     "born" → "bear"), "DELIVERED". Detected morphologically (auxpass) but the LEMMA class is DB-driven
#     so a tenant grows its own birth verbs freq-gated. DB-DOWN code-fallback seed below.
#   • offspring_noun (NOUNS): the offspring / newborn nouns a birth NAME binds to (son/daughter/baby/
#     boy/girl/twin/…). Doubles as a keyed map (SAME rail as kinship_noun): a row whose ``description``
#     == 'birth' is a SELF-GATING newborn noun (baby/newborn/infant) whose mere presence marks the
#     clause as a birth event ("had a BABY boy named Jasper" — no born-verb needed). A generic
#     offspring noun (son/boy/twin) only anchors a newborn name INSIDE an already-natal clause (a
#     born-verb OR a birth-marker present), so "my son likes soccer" is never mis-typed as a baby.
_BOOTSTRAP_NATAL_PREDICATES: frozenset[str] = frozenset({"bear", "deliver"})
NATAL_PREDICATE_CATEGORY = "natal_predicate"

_BOOTSTRAP_OFFSPRING_NOUNS: frozenset[str] = frozenset({
    "baby", "newborn", "infant", "son", "daughter", "child", "kid", "boy", "girl",
    "twin", "triplet", "grandson", "granddaughter", "grandchild", "nephew", "niece",
})
# The SELF-GATING newborn nouns (description='birth'): their mere presence marks a birth event.
_BOOTSTRAP_OFFSPRING_BIRTH_MAP: dict[str, str] = {
    "baby": "birth", "newborn": "birth", "infant": "birth",
}
OFFSPRING_NOUN_CATEGORY = "offspring_noun"

# Per-category DB-DOWN fallback seed. resolve_cues consults this when a category resolves empty / the
# read fails, so EVERY category fails safe to its own evidenced floor (never the wrong class, never
# empty). naming_verb keeps its dedicated bootstrap for back-compat with resolve_naming_verbs.
_BOOTSTRAP_BY_CATEGORY: dict[str, frozenset[str]] = {
    NAMING_VERB_CATEGORY: _BOOTSTRAP_NAMING_VERBS,
    # MUST be registered: `_bootstrap_for` falls back to _BOOTSTRAP_NAMING_VERBS for an
    # unregistered category, which would resolve call/dub/christen as naming NOUNS on a cold tenant.
    NAMING_NOUN_CATEGORY: _BOOTSTRAP_NAMING_NOUNS,
    LVC_SUPPORT_VERB_CATEGORY: _BOOTSTRAP_LVC_SUPPORT_VERBS,
    INCHOATIVE_VERB_CATEGORY: _BOOTSTRAP_INCHOATIVE_VERBS,
    ASPECTUAL_CONTROL_VERB_CATEGORY: _BOOTSTRAP_ASPECTUAL_CONTROL_VERBS,
    CESSATIVE_VERB_CATEGORY: _BOOTSTRAP_CESSATIVE_VERBS,
    IMPLICATIVE_VERB_CATEGORY: _BOOTSTRAP_IMPLICATIVE_VERBS,
    CONTINUATIVE_ADVERB_CATEGORY: _BOOTSTRAP_CONTINUATIVE_ADVERBS,
    EXEMPLIFICATION_MARKER_CATEGORY: _BOOTSTRAP_EXEMPLIFICATION_MARKERS,
    ACQUISITION_VERB_CATEGORY: _BOOTSTRAP_ACQUISITION_VERBS,
    POSSESSION_VERB_CATEGORY: _BOOTSTRAP_POSSESSION_VERBS,
    EMPLOYMENT_VERB_CATEGORY: _BOOTSTRAP_EMPLOYMENT_VERBS,
    RELOCATION_VERB_CATEGORY: _BOOTSTRAP_RELOCATION_VERBS,
    PROBLEM_NOUN_CATEGORY: _BOOTSTRAP_PROBLEM_NOUNS,
    POSITION_NOUN_CATEGORY: _BOOTSTRAP_POSITION_NOUNS,
    SVO_PARTICLE_CATEGORY: _BOOTSTRAP_SVO_PARTICLES,
    RELATIONAL_NOUN_CATEGORY: _BOOTSTRAP_RELATIONAL_NOUNS,
    # MUST be registered even though the floor is EMPTY: `_bootstrap_for` falls back to
    # _BOOTSTRAP_NAMING_VERBS for an unregistered category, so an unregistered attribute_noun would
    # resolve the NAMING-VERB lemmas as attribute nouns on every cold/empty tenant.
    ATTRIBUTE_NOUN_CATEGORY: _BOOTSTRAP_ATTRIBUTE_NOUNS,
    DISCOURSE_MARKER_CATEGORY: _BOOTSTRAP_DISCOURSE_MARKERS,
    KINSHIP_NOUN_CATEGORY: _BOOTSTRAP_KINSHIP_NOUNS,
    IDENTIFIER_NOUN_CATEGORY: _BOOTSTRAP_IDENTIFIER_NOUNS,
    LOCATIVE_PARTICIPLE_CATEGORY: _BOOTSTRAP_LOCATIVE_PARTICIPLES,
    SHELL_NOUN_CATEGORY: _BOOTSTRAP_SHELL_NOUNS,
    NATAL_PREDICATE_CATEGORY: _BOOTSTRAP_NATAL_PREDICATES,
    OFFSPRING_NOUN_CATEGORY: _BOOTSTRAP_OFFSPRING_NOUNS,
    MEASURE_VERB_CATEGORY: _BOOTSTRAP_MEASURE_VERBS,
    DOSAGE_NOUN_CATEGORY: _BOOTSTRAP_DOSAGE_NOUNS,
    MEASURE_NOUN_CATEGORY: _BOOTSTRAP_MEASURE_NOUNS,
    # THIN_TYPE_CATEGORY is intentionally NOT here: it is a keyed-value (surface→type) class resolved
    # by resolve_thin_type() into a dict, not a flat cue set. Its DB-DOWN fallback is
    # _BOOTSTRAP_THIN_TYPE_MAP, applied in resolve_thin_type().
}


def _bootstrap_for(category: str) -> frozenset[str]:
    """The DB-DOWN code-fallback seed for `category`. Returns that category's own evidenced floor.

    ⚠️ AN UNREGISTERED CATEGORY RETURNS EMPTY AND LOGS CRITICAL — it does NOT fall back to the
    naming-verb set. The old default (`_BOOTSTRAP_NAMING_VERBS`) meant any category missing from
    `_BOOTSTRAP_BY_CATEGORY` silently resolved the NAMING verbs (name/call/dub/christen/…) as its
    own class on every cold or DB-down tenant. A WRONG floor is strictly worse than an empty one:
    an empty floor makes the consumer decline (an honest miss the tenant then grows past), while a
    wrong floor makes it FIRE on the wrong lemmas — e.g. an unregistered `social_role` would treat
    "call"/"dub" as social roles. Two `_BOOTSTRAP_BY_CATEGORY` entries carried standing comments
    saying they existed ONLY to dodge this default (naming_noun, attribute_noun); that is the
    signature of a defaulting rule that should never have defaulted. Registered categories with a
    deliberately EMPTY floor (attribute_noun) are unaffected — they were already returning empty.
    """
    floor = _BOOTSTRAP_BY_CATEGORY.get(category)
    if floor is None:
        log.critical("linguistic_cue_overlay.unregistered_cue_category",
                     category=category,
                     detail="no code floor registered in _BOOTSTRAP_BY_CATEGORY; "
                            "resolving EMPTY (never the naming-verb set)")
        return frozenset()
    return floor


def _fetch_cues(dsn: str, schema_qualifier: str, category: str) -> frozenset[str]:
    """Read ACTIVE cue lemmas of `category` from a single explicit schema. `schema_qualifier` is a
    bare, already-validated schema identifier ('public' or 'faultline_<slug>'). Returns a frozenset
    of lowercased cue lemmas. Raises on a missing table / read error so the caller's fail-safe
    (bootstrap) applies."""
    cues: set[str] = set()
    # connect_timeout (CONNECTION guard, NOT an LLM/op timeout): a momentarily-slow PG must not block
    # a turn unboundedly on a cold cue read. On timeout/failure psycopg2 raises → the caller's
    # fail-safe (bootstrap cue set) applies; correctness is preserved.
    # read_only_connection (src/api/db_read.py): autocommit + readonly + guaranteed close.
    # A metadata read must never own a transaction (AccessShareLock held across a slow
    # caller stalled prod deprovision + pg_dump) and never own a backend past its scope.
    with read_only_connection(dsn, connect_timeout=5) as conn:
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
        category's own BOOTSTRAP floor — we do NOT read public for a bound tenant (isolation).

    ⚠️ THE RESULT CAN BE EMPTY IN EXACTLY TWO CASES, both correct and both honest: a category whose
    registered floor is DELIBERATELY empty (attribute_noun), and a category that is NOT REGISTERED in
    `_BOOTSTRAP_BY_CATEGORY` at all (which also logs critical — see `_bootstrap_for`). This
    docstring used to claim the result is never empty; that was already untrue for attribute_noun,
    and the unregistered case must be empty rather than silently borrowing the naming-verb set.

    ⚠️ KNOWN RESIDUAL, NOT CHANGED HERE (it is the SET analogue of the keyed-map defect fixed in
    `_resolve_keyed_map`): when a tenant's rows for a category resolve EMPTY, this falls back to the
    WHOLE floor. So an operator who DEACTIVATES EVERY member of a set class does not disable it —
    the floor comes back. `_resolve_keyed_map` handles that correctly (a deactivated cue is
    subtracted from the floor); this resolver does not, and the fix belongs in a change that is
    allowed to alter set-resolver behaviour.
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


def resolve_cessative_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE CESSATIVE-aspect verb lemma set for the ContextVar-bound current
    request schema (tenant-only), via the SAME binding as the naming/lvc/inchoative/aspectual
    resolvers. Consumed by the intent router's LEXICAL-cessation branch against the grammar shape
    `linguistics.analyze_directive` reports. DELIBERATELY DISTINCT from the aspectual-control set
    (see `_BOOTSTRAP_CESSATIVE_VERB_SHAPES`). Fail-safe: never empty (the cessative_verb floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), CESSATIVE_VERB_CATEGORY)


def resolve_cessative_verb_shapes(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE cessative-verb -> ADMITTED COMPLEMENT SHAPES map (`|`-joined
    subset of {xcomp_progressive, direct_object, intransitive}) from the `description` column of the
    cessative_verb rows — the SAME set+map-on-one-rail shape exemplification_marker uses. A verb is
    only a cessation cue in the shapes its own row admits, so a polysemous transitive ("I stopped the
    car", "I folded the laundry") can never route destructive. Fail-safe: bootstrap floor
    (`_BOOTSTRAP_CESSATIVE_VERB_SHAPES`)."""
    return _resolve_keyed_map(dsn, CESSATIVE_VERB_CATEGORY, _BOOTSTRAP_CESSATIVE_VERB_SHAPES)


def resolve_implicative_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE IMPLICATIVE control-verb lemma set for the ContextVar-bound
    current request schema (tenant-only), via the SAME binding as the naming/lvc/inchoative/
    aspectual resolvers. Used by ``linguistics._implicative_control_xcomp`` to license descending
    into an INFINITIVAL complement whose truth the matrix ENTAILS ("I had to take it in" -> I took
    it in). Karttunen 1971. DELIBERATELY DISTINCT from the aspectual-control set, which licenses
    the opposite shape (progressive -ing, never infinitival).
    Fail-safe: never empty (the implicative_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), IMPLICATIVE_VERB_CATEGORY)


def resolve_aspectual_control_phases(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE aspectual-control verb -> PHASE-LABEL map from the
    `description` column (the SAME set+label-on-one-rail shape cessative_verb uses).

    The label is what tells a consumer whether negating the matrix scopes DOWN onto the complement
    (ingressive / continuative) or AFFIRMS it (terminative). Reading it here keeps the phase
    knowledge in the DB where the class actually grows, instead of re-deriving a verb list in code.
    Fail-safe: bootstrap floor (`_BOOTSTRAP_ASPECTUAL_CONTROL_PHASES`)."""
    return _resolve_keyed_map(dsn, ASPECTUAL_CONTROL_VERB_CATEGORY,
                              _BOOTSTRAP_ASPECTUAL_CONTROL_PHASES)


def resolve_implicative_polarities(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE implicative verb -> POLARITY-LABEL map from `description`.

    Distinguishes POSITIVE implicatives (negation scopes down onto the complement) from NEGATIVE
    ones (negation AFFIRMS the complement). Fail-safe: bootstrap floor
    (`_BOOTSTRAP_IMPLICATIVE_POLARITIES`)."""
    return _resolve_keyed_map(dsn, IMPLICATIVE_VERB_CATEGORY,
                              _BOOTSTRAP_IMPLICATIVE_POLARITIES)




def resolve_continuative_adverbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE CONTINUATIVE/CANCELLING comparative-adverb LEMMA set.

    Consumed by `linguistics._predicate_negated`: a negated comparative adverbial cancels its event
    ONLY for this closed set; every other negated comparative is comparative-scope and asserts its
    event. Fail-safe: never empty (the continuative_adverb floor) — and the consumer additionally
    treats an empty resolution as a fault, because an emptied ADMISSION class would silently stop
    every cancellation from registering."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), CONTINUATIVE_ADVERB_CATEGORY)


def resolve_exemplification_markers(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE EXEMPLIFICATION-marker set (Hearst hyponymy cues: "such as",
    "including", "like", "especially") for the ContextVar-bound current request schema (tenant-only),
    via the SAME binding as the sibling cue resolvers. Multi-word members are the space-joined
    lowercase surface. Used by ``linguistics._chain_exemplification``. Fail-safe: never empty (the
    exemplification_marker bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), EXEMPLIFICATION_MARKER_CATEGORY)


def resolve_exemplification_marker_modes(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE exemplification-marker → MODE map ({marker: 'unambiguous' |
    'comma_required'}) from the `description` column of the exemplification_marker rows — the SAME
    set+map-on-one-rail shape identifier_noun uses. 'comma_required' marks a POLYSEMOUS marker
    ("like") that only reads as exemplification under nonrestrictive comma apposition, so a manner
    adjunct ("I ate lunch like a king") never mints a hyponymy edge. Fail-safe: bootstrap floor
    (`_BOOTSTRAP_EXEMPLIFICATION_MARKER_MODES`)."""
    return _resolve_keyed_map(dsn, EXEMPLIFICATION_MARKER_CATEGORY,
                              _BOOTSTRAP_EXEMPLIFICATION_MARKER_MODES)


def resolve_acquisition_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE ACQUISITION / transfer-of-possession verb lemma set for the
    ContextVar-bound current request schema (tenant-only), via the SAME binding as the naming/lvc/
    inchoative/temporal resolvers. Used by ``linguistics.analyze_acquisition`` to recognize a dated
    "got/bought a <device>" coming-to-possess construction so the user→device ownership linkage is
    EXPOSED as an inferred, dated edge. ⚠️ FLAGGED bounded lexical class (see
    ``_BOOTSTRAP_ACQUISITION_VERBS``). Fail-safe: never empty (the acquisition_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), ACQUISITION_VERB_CATEGORY)


def resolve_possession_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE STATIVE-POSSESSION verb lemma set for the ContextVar-bound current
    request schema (tenant-only), via the SAME binding as the naming/lvc/acquisition/temporal
    resolvers. Used by ``linguistics._type_is_self_possessed`` (the named-instance self-possession
    gate) to decide whether a named instance's TYPE belongs to the speaker — "I own a motorcycle named
    Bolt" / "I have a dog named Rex" — before the possession edge is minted. DISTINCT from the
    ACQUISITION class (stative CURRENTLY-possessing vs transfer COMING-to-possess). ⚠️ FLAGGED bounded
    lexical class (see ``_BOOTSTRAP_POSSESSION_VERBS``). Fail-safe: never empty (the possession_verb
    bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), POSSESSION_VERB_CATEGORY)


def resolve_employment_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE EMPLOYMENT / role-predication verb lemma set for the ContextVar-
    bound current request schema (tenant-only), via the SAME binding as the naming/lvc/acquisition/
    possession/temporal resolvers. Used by ``linguistics.derive_sentence_facts``'s ``_chain_employment``
    to recognize the "<subject> <employment verb> as <role> [at|for <org>]" construction —
    occupation(<subject>, <role>) + works_for(<subject>, <org>) — so "I work as a nurse at the clinic",
    "she serves as treasurer", "employed as an engineer at Globex" all land. The cue class IS the safety
    gate that lets the chain be broad without over-capturing ("dressed as a pirate" / "known as X" — the
    verb is NOT in this class → NOT an occupation). ⚠️ FLAGGED bounded lexical class (see
    ``_BOOTSTRAP_EMPLOYMENT_VERBS``). Fail-safe: never empty (the employment_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), EMPLOYMENT_VERB_CATEGORY)


def resolve_relocation_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE RELOCATION / change-of-residence verb lemma set for the
    ContextVar-bound current request schema (tenant-only), via the SAME binding as the naming/lvc/
    employment/temporal resolvers. Used by ``linguistics.derive_sentence_facts``'s ``_chain_relocation``
    to recognize the "<person> <relocation verb> to <place>" construction — ``lives_in(<subject>,
    <place>)`` as a state change (new current residence) — so "I moved to Tokyo", "she relocated to
    Berlin" all land the residence linkage the present-tense "live in X" path already produces. The cue
    class IS the safety gate that lets the chain be broad without over-capturing ("move to the next
    item" / "move the box" — the parse's PERSON-subject + PLACE-destination gate rejects those). ⚠️
    FLAGGED bounded lexical class (see ``_BOOTSTRAP_RELOCATION_VERBS``). Fail-safe: never empty (the
    relocation_verb bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), RELOCATION_VERB_CATEGORY)


def resolve_locative_participles(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE LOCATIVE-PARTICIPLE lemma set for the ContextVar-bound current
    request schema (tenant-only), via the SAME binding as the naming/relocation/temporal resolvers.
    Used by ``linguistics.derive_sentence_facts``'s locative pre-pass / ``_chain_copula_locative`` to
    recognize the copula/passive CONTAINMENT idiom "<X> is <participle> in/at/on/within/inside <place>"
    → ``located_in(<X>, <place>)`` (the seeded containment hierarchy rel). The cue class is the safety
    gate that keeps the reading off a non-locative passive+prep ("is written in Python"). ⚠️ FLAGGED
    bounded lexical class (see ``_BOOTSTRAP_LOCATIVE_PARTICIPLES``). Fail-safe: never empty (the
    locative_participle bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), LOCATIVE_PARTICIPLE_CATEGORY)


def resolve_problem_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE PROBLEM-NOUN (bland eventive head) lemma set for the ContextVar-
    bound current request schema (tenant-only), via the SAME binding as the naming/lvc/acquisition/
    temporal resolvers. Used by ``linguistics.analyze_events`` (the with-PP state lane) to recognize a
    SEMANTICALLY-EMPTY problem head ("had an issue/problem/trouble WITH X") so a competing
    ``(<affected>, has_state, <problem-state>)`` candidate is emitted alongside the participated_in
    candidate (Stage-2 arbitration picks the strong state reading). ⚠️ FLAGGED bounded lexical class
    (see ``_BOOTSTRAP_PROBLEM_NOUNS``). Fail-safe: never empty (the problem_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), PROBLEM_NOUN_CATEGORY)


def resolve_position_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE POSITION-NOUN (job/role container head) lemma set for the
    ContextVar-bound current request schema (tenant-only), via the SAME binding as the naming/
    employment resolvers. Used by ``linguistics.derive_sentence_facts`` (the possessed-position-noun
    frame) to recognize "my <ROLE> as <occupation> [at|for <org>]" — the noun-headed twin of the
    ``employment_verb`` "as <role>" construction — so the occupation lands even when the sentence's
    main verb is unrelated. ⚠️ FLAGGED bounded lexical class (see ``_BOOTSTRAP_POSITION_NOUNS``).
    Fail-safe: never empty (the position_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), POSITION_NOUN_CATEGORY)


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


def resolve_attribute_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE ATTRIBUTE-noun set for the ContextVar-bound current request
    schema (tenant-only), via the SAME binding as the naming/relational/kinship resolvers. Used by the
    possessive-attribute copula binding to decide whether a SINGLE-WORD ADJECTIVAL complement is a
    scalar VALUE of this attribute ("<possessor>'s <attribute-noun> is <adjective>") or belongs to the
    preference seam. Mirrors ``resolve_relational_nouns`` exactly, with ONE deliberate difference:

    ⚠️ THIS CLASS CAN LEGITIMATELY BE **EMPTY** — the bootstrap floor is empty by design (every
    member is domain vocabulary; see ATTRIBUTE_NOUN_CATEGORY). Callers MUST treat an empty result as
    "nothing is a known attribute noun yet" and CONTAIN the construction (no entity mint, no value
    capture) while proposing the noun on the growth queue — never as a reason to fall back to
    capturing by value shape alone."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), ATTRIBUTE_NOUN_CATEGORY)


def resolve_naming_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE NAMING-NOUN set for the ContextVar-bound current request schema
    (tenant-only), via the SAME binding as the naming-verb/kinship resolvers. Used by the copular
    naming-frame detector: a copula whose ``nsubj`` lemma is in this set ("<bearer>'s name/nickname/
    alias is X") is a NAMING frame, so the guard can decide WHOSE name it is. Fail-safe: never empty
    (the naming_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), NAMING_NOUN_CATEGORY)


def resolve_kinship_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE KINSHIP-noun set for the ContextVar-bound current request schema
    (tenant-only), via the SAME binding as the naming/rel_type/temporal resolvers. Used by the
    genitive-possessive deriver's inherent-relation pick: a relational noun IN this set is a
    person↔person kinship link (``related_to``); NOT in it is component/part mereology (``part_of``).
    Fail-safe: never empty (the kinship_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), KINSHIP_NOUN_CATEGORY)


def resolve_natal_predicates(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE NATAL-PREDICATE (birth-verb) lemma SET for the ContextVar-bound
    current request schema (tenant-only), via the SAME binding as the naming/kinship resolvers. Used
    by the deriver's ``_chain_natal_birth``: a passive-marked verb whose lemma is in this set ("was
    BORN"→bear, "DELIVERED") denotes a birth event. Fail-safe: never empty (natal_predicate floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), NATAL_PREDICATE_CATEGORY)


def resolve_offspring_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE OFFSPRING-NOUN SET for the ContextVar-bound current request schema
    (tenant-only). Used by ``_chain_natal_birth`` to find the noun a newborn NAME binds to (son/
    daughter/baby/boy/girl/twin/…). Fail-safe: never empty (the offspring_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), OFFSPRING_NOUN_CATEGORY)


def resolve_offspring_birth_markers(dsn: str) -> frozenset[str]:
    """Resolve the SELF-GATING newborn-noun subset (offspring_noun rows whose ``description``=='birth':
    baby/newborn/infant) — the nouns whose mere PRESENCE marks a clause as a birth event (no born-verb
    needed). Reads the same offspring_noun rows as ``resolve_offspring_nouns`` but filters on the
    description column (SAME set+map rail as kinship_noun). Fail-safe: bootstrap floor
    (``_BOOTSTRAP_OFFSPRING_BIRTH_MAP``)."""
    _m = _resolve_keyed_map(dsn, OFFSPRING_NOUN_CATEGORY, _BOOTSTRAP_OFFSPRING_BIRTH_MAP)
    return frozenset(k for k, v in (_m or {}).items() if (v or "").strip().lower() == "birth")


def resolve_shell_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE SHELL-NOUN (generic abstract anaphoric head) set for the
    ContextVar-bound current request schema (tenant-only), via the SAME binding as the naming/kinship/
    temporal resolvers. Used by the cross-sentence discourse-topic coref
    (derive_sentence_facts._topic_definite_subject): a DEFINITE subject NP whose head is in this set,
    with no closer antecedent, co-refers with the turn's topic and binds to it — consolidating a later
    generic-shell description ("the flaw"/"the ruling"/"the condition") that GLiNER2 cannot coarse-match
    to the topic's exact type noun. Fail-safe: never empty (the shell_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), SHELL_NOUN_CATEGORY)


def _fetch_thin_type_map(dsn: str, schema_qualifier: str) -> dict[str, str]:
    """Read the ACTIVE thin-type (surface→type) MAP from a single explicit schema. Mirrors
    `_fetch_cues` but returns a {surface: type} dict: `cue` is the surface head lemma, `description`
    is the coarse target type. `schema_qualifier` is a bare, already-validated schema identifier
    ('public' or 'faultline_<slug>'). Raises on a missing table / read error so the caller's
    fail-safe (the bootstrap map) applies. A row with an empty/NULL description is skipped (a thin
    type with no target carries no slot tag)."""
    out: dict[str, str] = {}
    # read_only_connection (src/api/db_read.py): autocommit + readonly + guaranteed close.
    # A metadata read must never own a transaction (AccessShareLock held across a slow
    # caller stalled prod deprovision + pg_dump) and never own a backend past its scope.
    with read_only_connection(dsn, connect_timeout=5) as conn:
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

    ⚠️ RESOLUTION IS **FLOOR ∪ ROWS, ROWS WIN ON KEY COLLISION** — NOT replace. This used to read
    ``if not tenant_map: tenant_map = dict(bootstrap)``, i.e. the code floor applied ONLY when the
    tenant held ZERO rows in the category. The moment a tenant grew ONE row, the ENTIRE seeded floor
    was discarded. Measured locally, exactly as on production: tenant
    ``faultline_<seat-uuid>`` holds social_role = {agent, colleague} and
    therefore resolved WITHOUT the floor's ``friend → friend_of`` — so "my friend is Sam" fell back
    to has_role + ``owns(user, sam)``, a PERSON filed as an owned object, on a tenant that had done
    nothing wrong except grow. A growth mechanism that ERODES seeded knowledge as the tenant grows
    gets worse the more the product is used; that is the opposite of the documented contract
    (CLAUDE.md: the overlays resolve "seed ∪ tenant rows (tenant overrides)").

    AUTHORITY ORDER (user > seed > growth) is preserved by the direction of the merge: the floor is
    the base, tenant rows are applied OVER it, so growth may ADD keys and may OVERRIDE a floor key,
    but the floor can never override a tenant row. The floor can only ever WIDEN a resolution, never
    narrow or change one the tenant has an opinion about.

    WHY THIS IS NOT A CROSS-TENANT SEAM: the `bootstrap` argument is an IN-CODE constant, not another
    tenant's rows and not `public`. A bound tenant still never reads `public` here. The sibling
    overlays (`rel_type_overlay`, `taxonomy_overlay`) legitimately read TENANT-ONLY because
    provisioning COPIES `public` into the tenant schema, so their "seed ∪ tenant" is realised at
    provisioning time. That equivalence does NOT hold for these keyed cue classes: several floors
    (social_role, cessative_verb, continuative_adverb, problem_noun, relational_noun, discourse_marker)
    have NO rows in `public.linguistic_cues` at all, so provisioning cannot have copied them and the
    in-code floor is the ONLY carrier. Replace semantics therefore deleted knowledge that had nowhere
    else to live.
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
            # FLOOR ∪ PUBLIC ROWS, rows win, minus what public has DEACTIVATED. Same merge direction
            # and same suppression rule as the tenant branch below, so the unscoped (boot/anonymous)
            # path cannot resolve a WIDER or NARROWER map than a bound tenant would.
            _rows, _suppressed = _fetch_keyed_map_with_suppressions(dsn, "public", category)
            fresh = {k: v for k, v in bootstrap.items() if k not in _suppressed}
            fresh.update(_rows)
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
        # FLOOR ∪ TENANT ROWS, TENANT WINS ON KEY COLLISION (see the docstring). The floor is the
        # base and the tenant rows are applied over it, so growth ADDS/OVERRIDES and the floor can
        # only ever widen — it can never overrule a row the tenant actually holds. A cue the tenant
        # has DEACTIVATED is dropped from the floor first: the floor speaks only where the tenant is
        # SILENT, so a user correction (is_active=false) is never resurrected by the code seed.
        _rows, _suppressed = _fetch_keyed_map_with_suppressions(dsn, schema_name, category)
        tenant_map = {k: v for k, v in bootstrap.items() if k not in _suppressed}
        tenant_map.update(_rows)
    except Exception as e:  # noqa: BLE001 — fail-safe; do NOT read public for a bound tenant
        log.warning("linguistic_cue_overlay.keyed_map_tenant_fetch_failed",
                    schema=schema_name, category=category, error=str(e)[:160])
        return dict(bootstrap)
    with _lock:
        _keyed_map_cache[cache_key] = {"map": tenant_map, "loaded_at": time.time()}
    return tenant_map


def _fetch_keyed_map_with_suppressions(
    dsn: str, schema_qualifier: str, category: str
) -> tuple[dict[str, str], frozenset[str]]:
    """Read the keyed map of `category` from ONE explicit schema AND the set of cues the schema has
    explicitly DEACTIVATED. One query, two products — no extra round trip.

    ⚠️ THE SUPPRESSION SET IS WHAT MAKES THE `_resolve_keyed_map` UNION SAFE, and without it the
    union would be a REGRESSION. `linguistic_cues` has no user-facing DELETE path: the documented
    correction for a cue is ``UPDATE linguistic_cues SET is_active = false`` (spelled out at
    ``src/extraction/linguistics.py:6929``), and the seed migrations are all
    ``ON CONFLICT (cue, category) DO NOTHING`` precisely so a re-run cannot blow over a corrected
    row. But an in-code floor has no ``is_active`` column. So a plain floor ∪ active-rows union would
    RESURRECT, from the code floor, exactly the seeded cue the user just switched off — silently
    undoing the only correction mechanism the class has.

    THE RULE: **the floor speaks only where the tenant is SILENT.** A row that exists and is
    deactivated is not silence — it is an opinion, and it outranks the floor (authority order:
    user > seed > growth). Returned suppressions are subtracted from the floor by the caller.

    Scope of "deactivated" is exactly the inverse of the existing active filter (``is_active =
    false``), so no new semantics are introduced. ``archived_at`` is NOT consulted here because no
    resolver in this module has ever consulted it (``_fetch_cues`` / ``_fetch_keyed_map`` filter on
    ``is_active`` alone); measured on this database, 0 rows carry ``archived_at``, so the two
    predicates do not currently disagree. If ``archived_at`` ever becomes an independent retirement
    signal it must be added to BOTH the active filter and this predicate in the same change.
    """
    active: dict[str, str] = {}
    suppressed: set[str] = set()
    with read_only_connection(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT cue, description, is_active FROM {schema_qualifier}.linguistic_cues "
                f"WHERE category = %s",
                (category,),
            )
            for (cue, desc, is_active) in cur.fetchall():
                if not cue or not cue.strip():
                    continue
                key = cue.strip().lower()
                if is_active:
                    if desc and desc.strip():
                        active[key] = desc.strip().lower()
                else:
                    suppressed.add(key)
    return active, frozenset(suppressed)


def _fetch_keyed_map(dsn: str, schema_qualifier: str, category: str) -> dict[str, str]:
    """Read the ACTIVE (cue→description) MAP of `category` from a single explicit schema. Mirrors
    `_fetch_thin_type_map` but is category-parameterized. Raises on a missing table / read error so the
    caller's fail-safe applies. A row with an empty/NULL description is skipped (no mapping)."""
    out: dict[str, str] = {}
    # read_only_connection (src/api/db_read.py): autocommit + readonly + guaranteed close.
    # A metadata read must never own a transaction (AccessShareLock held across a slow
    # caller stalled prod deprovision + pg_dump) and never own a backend past its scope.
    with read_only_connection(dsn, connect_timeout=5) as conn:
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


def resolve_identifier_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE IDENTIFIER-CONTEXT noun SET for the ContextVar-bound current
    request schema (tenant-only), via the SAME binding as the kinship/naming/temporal resolvers. Used
    by the deriver's context-signalled scalar chain to recognise "my <ticket/case/…> [number] is
    <value>". Fail-safe: never empty (the identifier_noun bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), IDENTIFIER_NOUN_CATEGORY)


def resolve_identifier_noun_roles(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE identifier-noun → ROLE MAP for the ContextVar-bound current
    request schema. Reads the `description` column of the identifier_noun rows ({noun: 'strong'|
    'suffix'}). 'strong' = establishes identifier context alone or as a compound; 'suffix' = the
    ambiguous generic tail ("number") that only rides alongside a strong cue. Metadata-driven, NOT an
    in-code literal. Same contract as resolve_kinship_rel_map. Fail-safe: bootstrap floor
    (`_BOOTSTRAP_IDENTIFIER_NOUN_ROLE_MAP`)."""
    return _resolve_keyed_map(dsn, IDENTIFIER_NOUN_CATEGORY, _BOOTSTRAP_IDENTIFIER_NOUN_ROLE_MAP)


def resolve_unit_scalar_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE measurement-unit → scalar rel_type MAP for the ContextVar-bound
    current request schema. Reads the unit_scalar rows ({unit: rel_type}). Used by the copula
    measurement chain so "she is 62 years old" → unit 'year' → age (a SCALAR rel routed to
    entity_attributes). Same contract as resolve_thin_type. Fail-safe: bootstrap floor
    (`_BOOTSTRAP_UNIT_SCALAR_MAP`)."""
    return _resolve_keyed_map(dsn, UNIT_SCALAR_CATEGORY, _BOOTSTRAP_UNIT_SCALAR_MAP)


def resolve_measure_verbs(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE MEASUREMENT-verb lemma set for the ContextVar-bound current
    request schema (tenant-only). Uses the SAME binding as the naming / unit_scalar / natal
    resolvers. Used by the VERB-MEASURE scalar lane's mis-tag arm (linguistics
    ``VERB_MEASURE_SCALAR`` V2) to license the measure reading of a ROOT token spaCy tagged NOUN
    ("An adult blue-ringed octopus measures about 5 centimeters across." → 'measures' NNS ROOT).
    Fail-safe: never empty (the ``_BOOTSTRAP_MEASURE_VERBS`` bootstrap floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), MEASURE_VERB_CATEGORY)


def resolve_dosage_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE dosage-FAMILY noun set for the ContextVar-bound current
    request schema (tenant-only). Uses the SAME binding as the measure_verb / kinship resolvers.
    Drives the ONE dosage-family resolution across the three seams (issue #18): the ingest
    copula-weld rebind (linguistics ``SPINE_DOSAGE_FAMILY``), the correction addressing family
    rung (``_address_scalar_target``), and the query mirror-frame's aspect admission.
    Fail-safe: never empty (the ``_BOOTSTRAP_DOSAGE_NOUNS`` bootstrap floor — the five members
    the owner data call discharged)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), DOSAGE_NOUN_CATEGORY)


def resolve_dosage_canonical_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE dosage-noun → CANONICAL ATTRIBUTE MAP for the ContextVar-bound
    current request schema. Reads the `description` column of the dosage_noun rows
    ({noun: canonical attribute}) — the keyed-map contract, same rail as kinship_rel_map. The seed
    canonicalizes every member to ``quantity`` (the attribute the #14 take-frame already writes),
    so all three seams resolve the family to ONE stored name. A tenant row may point a member at a
    different canonical (tenant rows WIN on key collision; the floor is only ever widened — the
    established floor∪tenant merge rule). Fail-safe: bootstrap floor
    (`_BOOTSTRAP_DOSAGE_CANONICAL_MAP`)."""
    return _resolve_keyed_map(dsn, DOSAGE_NOUN_CATEGORY, _BOOTSTRAP_DOSAGE_CANONICAL_MAP)


def resolve_measure_nouns(dsn: str) -> frozenset[str]:
    """Resolve the per-tenant ACTIVE measurement-family noun set (BEYOND dosage, issue #19 W1)
    for the ContextVar-bound current request schema. Same binding/merge contract as
    ``resolve_dosage_nouns``. Drives the generalized possessive-quantity rebind at the ingest
    seam (linguistics ``SPINE_POSSESSED_QUANTITY_L4``) and the query mirror-frame's family
    admission. Fail-safe: never empty (the ``_BOOTSTRAP_MEASURE_NOUNS`` floor)."""
    return resolve_cues(dsn, rel_type_overlay.get_current_schema(), MEASURE_NOUN_CATEGORY)


def resolve_measure_canonical_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE measure-noun → CANONICAL ATTRIBUTE MAP for the
    ContextVar-bound current request schema ({noun: canonical} in the rows' `description`).
    Same keyed-map contract as ``resolve_dosage_canonical_map``: floor ∪ tenant, tenant wins on
    key collision, a user-retired cue stays suppressed (the floor is filtered by the
    suppression set before the union). Fail-safe: bootstrap floor
    (`_BOOTSTRAP_MEASURE_CANONICAL_MAP`)."""
    return _resolve_keyed_map(dsn, MEASURE_NOUN_CATEGORY, _BOOTSTRAP_MEASURE_CANONICAL_MAP)


def resolve_kinship_gender_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE kinship-noun → gender MAP for the ContextVar-bound current
    request schema. Reads the kinship_gender rows ({noun: gender}). Used by the named-instance binding
    chain so "a son Alex" → son → male → (alex, has_gender, male). Metadata-driven (NOT an in-code
    literal); a noun OUTSIDE the map mints no gender (a gender-neutral kin role like child/parent/
    sibling is absent → no fabricated gender). Same contract as resolve_unit_scalar_map. Fail-safe:
    bootstrap floor (`_BOOTSTRAP_KINSHIP_GENDER_MAP`)."""
    return _resolve_keyed_map(dsn, KINSHIP_GENDER_CATEGORY, _BOOTSTRAP_KINSHIP_GENDER_MAP)


def resolve_social_role_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE social-role-noun → rel_type MAP for the ContextVar-bound current
    request schema. Reads the social_role rows ({noun: rel_type}). Used by the named-instance binding
    chain so "a friend Sam" → friend → friend_of (a PERSON social tie, never ``owns``). Metadata-
    driven; a role OUTSIDE the map falls to a generic role slot (never a fabricated social tie). Same
    contract as resolve_kinship_gender_map. Fail-safe: bootstrap floor (`_BOOTSTRAP_SOCIAL_ROLE_MAP`)."""
    return _resolve_keyed_map(dsn, SOCIAL_ROLE_CATEGORY, _BOOTSTRAP_SOCIAL_ROLE_MAP)


def resolve_alias_predicate_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE phrasal-alias-predicate → licensing-particle MAP for the
    ContextVar-bound current request schema. Reads the alias_predicate rows ({verb_lemma: particle}).
    Used by the third-party nickname/alias deriver chain so "she goes by Dee" (go→'by') / "he is known
    as Sammy" (know→'as') bind (person, also_known_as, <Name>). The value is the licensing preposition
    the verb must govern with a PROPER-NOUN pobj for the alias reading — the disambiguator that keeps a
    non-naming same-verb use ("go to work") out. Metadata-driven (seeded migration 146, grown
    per-tenant), NOT an in-code verb literal. Same contract as resolve_role_noun_map. Fail-safe:
    bootstrap floor (`_BOOTSTRAP_ALIAS_PREDICATE_MAP`)."""
    return _resolve_keyed_map(dsn, ALIAS_PREDICATE_CATEGORY, _BOOTSTRAP_ALIAS_PREDICATE_MAP)


def resolve_role_noun_map(dsn: str) -> dict[str, str]:
    """Resolve the per-tenant ACTIVE role-noun → rel_type MAP for the ContextVar-bound current
    request schema. Reads the role_noun rows ({noun: rel_type}, USER→FILLER direction — see
    ``_BOOTSTRAP_ROLE_NOUN_MAP``). Used by the copula predicate-nominal role chain so "Globex
    Industries is my employer" binds the SUBJECT NP as the entity via the mapped rel (employer →
    works_for ⇒ (user, works_for, globex industries)) instead of minting (user, owns, "employer").
    Same contract as resolve_social_role_map. Fail-safe: bootstrap floor
    (`_BOOTSTRAP_ROLE_NOUN_MAP`)."""
    return _resolve_keyed_map(dsn, ROLE_NOUN_CATEGORY, _BOOTSTRAP_ROLE_NOUN_MAP)


# ── ALL-CATEGORY CUE SURFACES (the TOKENIZER-RECONCILIATION reader) ─────────────────
# Every resolver above is category-scoped because every CONSUMER is. This one is not: its consumer
# asks a question about the cue vocabulary AS A WHOLE — "is any cue surface destroyed before a
# consumer can ever see it?" (src/extraction/linguistics.py::_reconcile_cue_tokenizer_exceptions).
# A cue is matched by spaCy LEMMA/TEXT, so a surface the tokenizer SPLITS can never match anything
# and the row is silently DEAD (measured: seeded `id` → tokens ['i','d'], dead since migration 186).
# Same tenant/seed/TTL/fail-safe contract as ``resolve_cues``; category-agnostic by design.
_ALL_SURFACES_KEY = "__all_cue_surfaces__"


def _fetch_all_cue_surfaces(dsn: str, schema_qualifier: str) -> frozenset[str]:
    """Read every ACTIVE cue surface (all categories) from one explicit, already-validated schema."""
    surfaces: set[str] = set()
    with read_only_connection(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT cue FROM {schema_qualifier}.linguistic_cues "
                f"WHERE is_active = true"
            )
            for (cue,) in cur.fetchall():
                if cue and cue.strip():
                    surfaces.add(cue.strip().lower())
    return frozenset(surfaces)


def resolve_all_cue_surfaces(dsn: str) -> frozenset[str]:
    """Every ACTIVE cue surface across ALL categories for the ContextVar-bound schema (tenant-only;
    unbound → the public template). Fail-safe → EMPTY (unlike the category resolvers there is no
    bootstrap floor: the consumer's safe default is "reconcile nothing", i.e. today's tokenizer)."""
    if not dsn:
        return frozenset()
    schema_name = rel_type_overlay.get_current_schema()
    qualifier = schema_name.strip() if _is_real_tenant_schema(schema_name) else "public"
    cache_key = f"{qualifier}::{_ALL_SURFACES_KEY}"
    now = time.time()
    with _lock:
        entry = _overlay_cache.get(cache_key)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return entry["cues"]
    try:
        fresh = _fetch_all_cue_surfaces(dsn, qualifier)
    except Exception as e:  # noqa: BLE001 — fail-safe: reconcile nothing
        log.warning("linguistic_cue_overlay.all_cue_surfaces_fetch_failed",
                    schema=qualifier, error=str(e)[:160])
        return frozenset()
    with _lock:
        _overlay_cache[cache_key] = {"cues": fresh, "loaded_at": time.time()}
    return fresh


# ── CARVED-CLASS GROWTH ACCUMULATOR (request-scoped cue-candidate side-channel) ──────
# When a consumer of a CARVED cue class (social_role / problem_noun) sees the class's construction but
# the cue is NOT yet grown for this tenant, it DEGRADES to a generic walkable rel AND records the cue
# as a growth CANDIDATE here. The deriver/consumer cannot write to the DB itself (it is pure / has no
# connection), so candidates accumulate on a REQUEST-SCOPED ContextVar; the ingest/harvest seam drains
# them once (``drain_cue_candidates``) and writes them to ``<tenant>.ontology_evaluations`` (the SAME
# growth queue the rel_type / concept paths reuse, marked extraction_method='linguistic_cue_candidate'
# so the rel-type evaluator's firewall skips them). The re_embedder freq-gates (≥3) and grows them into
# ``<tenant>.linguistic_cues``. ContextVar (not a global list) so candidates never leak across requests
# or tenants. Bounded (a cap) so a non-draining caller can never grow it unboundedly.
import contextvars  # noqa: E402 — local to this growth seam

_cue_candidates: "contextvars.ContextVar[list]" = contextvars.ContextVar(
    "_linguistic_cue_candidates", default=None)
_CUE_CANDIDATE_CAP = 64


def record_cue_candidate(cue: str, category: str) -> None:
    """Record a CARVED-CLASS growth candidate (cue lemma, category) for the current request. Fail-safe:
    never raises (a growth-signal miss must never break extraction). De-dups within the request and is
    bounded by ``_CUE_CANDIDATE_CAP`` so a non-draining caller cannot accumulate unboundedly."""
    try:
        cue = (cue or "").strip().lower()
        category = (category or "").strip().lower()
        if not cue or not category:
            return
        lst = _cue_candidates.get()
        if lst is None:
            lst = []
            _cue_candidates.set(lst)
        if len(lst) >= _CUE_CANDIDATE_CAP:
            return
        pair = (cue, category)
        if pair not in lst:
            lst.append(pair)
    except Exception:  # noqa: BLE001 — fail-safe
        return


def drain_cue_candidates() -> list:
    """Return and CLEAR the request's accumulated cue candidates (list of (cue, category) tuples).
    Fail-safe → empty list. The caller writes them to the per-tenant growth queue."""
    try:
        lst = _cue_candidates.get()
        _cue_candidates.set(None)
        return list(lst) if lst else []
    except Exception:  # noqa: BLE001 — fail-safe
        return []


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
