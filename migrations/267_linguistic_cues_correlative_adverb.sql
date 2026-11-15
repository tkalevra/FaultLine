-- Migration 267: linguistic_cues — seed the CORRELATIVE-COMPARATIVE ADVERB cue class
-- Date: 2026-08-25
--
-- ⚠️⚠️ SUPERSEDED BY MIGRATION 269 — DO NOT EXTEND THIS CLASS, AND DO NOT FOLLOW THE RESOLVER
-- REFERENCE BELOW (`resolve_correlative_adverbs` has been DELETED along with the class).
-- This class enumerated the OPEN side of the division. The real split is CONTINUATIVE/CANCELLING
-- ("no longer", "no more" — CLOSED, two members) versus COMPARATIVE-SCOPE (every other negated
-- comparative — OPEN and PRODUCTIVE, event ASSERTED). Enumerating the open side can only ever
-- cover what somebody thought of, and each forgotten member is a live stored falsehood; that is
-- how five successive rounds each shipped one. Migration 269 seeds the CLOSED side instead, which
-- is complete by construction and subsumes this class entirely — a non-continuative lemma is now
-- rejected regardless of membership here. 269 deactivates these rows.
-- The reasoning below is kept because the four failed guard attempts it records are still the best
-- warning against re-deriving this distinction from the parse.
--
-- WHY
-- ---
-- "No longer did I drink coffee." is a CANCELLATION. "No sooner did I sit down than the phone rang."
-- ASSERTS that the sitting happened. The two are STRUCTURALLY IDENTICAL — both are fronted negated
-- comparative adverbials triggering subject-auxiliary inversion — and they mean opposite things
-- purely because of WHICH comparative adverb fills the slot: one encodes duration/continuation, the
-- other temporal sequence. No dependency label, POS tag or morphological feature encodes that
-- difference.
--
-- FOUR SYNTACTIC GUARDS WERE TRIED IN CODE BEFORE THIS CLASS EXISTED, AND ALL FOUR SHIPPED A
-- FALSEHOOD, each because it keyed on a property the WHOLE family shares:
--   1. comparative `Degree` — derived from the POS tag; measured 5/10 on one construction.
--   2. subject-auxiliary INVERSION — triggered by the entire class of fronted negative adverbials,
--      so it rejected the very cancellations the lane exists to capture and inverted a denial the
--      parent had captured correctly.
--   3. a `than` CHILD of the adverbial — misses the correlative, whose `than` attaches to the
--      subordinate clause, not to the adverb.
--   4. a clause-marking `than` anywhere in the predicate's subtree — present in EVERY comparative
--      clause of nonequivalence, so an unrelated relative clause ("… which pays less than Trantor
--      does") vetoed the cancellation; and the marker-vs-preposition label is itself unstable, so
--      the correlative it targeted slipped through half the time regardless.
--
-- The distinction is LEXICAL, and in this engine lexicon lives ONLY on the per-tenant cue rails —
-- never in code. Hence this class.
-- ⚠️ EDITABLE, NOT AUTO-GROWN — corrected from an earlier claim that it is "growable per tenant
-- exactly like every sibling class". It is NOT registered in the re_embedder's carved-growth
-- registry and has no candidate proposer, so nothing will ever grow into it automatically (a
-- candidate for an unregistered category resolves to 'cue_skipped'). What it IS is per-tenant
-- EDITABLE: an operator can add or deactivate a member and the overlay picks it up within its TTL.
-- That is the useful property for a small closed grammatical class, and registering it for growth
-- WITHOUT a proposer would create precisely the "built and dark" state that registry exists to
-- prevent.
--
-- FAIL-SAFE DIRECTION (deliberate): the consumer treats a lemma ABSENT from this class as NOT
-- correlative, so an unknown adverb can never veto a cancellation the engine would otherwise
-- capture correctly.
-- ⚠️ COST FRAMING CORRECTED: an earlier version of this comment said "under-seeding costs one
-- construction; over-seeding would invert real captures", implying an asymmetry that does not
-- exist. BOTH DIRECTIONS COST A STORED FALSEHOOD — under-seeding stores a DENIAL of an event the
-- speaker ASSERTED ("no sooner did I buy it" read as a cancellation), over-seeding stores an
-- AFFIRMATION of one they CANCELLED. The direction is chosen because CANCELLATIONS VASTLY
-- OUTNUMBER CORRELATIVES in ordinary use, not because one side is free.
--
-- `cue` is matched against the spaCy token LEMMA, lowercase (measured: "sooner" lemmatises to
-- "soon"). Consumed by linguistics._predicate_negated via
-- linguistic_cue_overlay.resolve_correlative_adverbs().

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('soon', 'correlative_adverb',
   'Correlative-comparative adverb: the fronted negated form ("no sooner … than …") ASSERTS its '
   'event rather than cancelling it. Structurally identical to a "no longer" cancellation; only '
   'the lexeme differs.',
   'No sooner did I sit down than the phone rang',
   'seed_correlative_adverb', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- Idempotent: ON CONFLICT DO NOTHING. Mirrors the fan-out in migration 113.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category = 'correlative_adverb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 267: correlative_adverb cues seeded into %', _schema;
    END LOOP;
END $$;
