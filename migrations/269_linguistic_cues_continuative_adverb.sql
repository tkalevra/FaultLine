-- Migration 269: linguistic_cues — seed the CONTINUATIVE / CANCELLING comparative-adverb class,
--                and retire the two enumeration-of-the-open-side classes it replaces.
-- Date: 2026-08-25
--
-- WHY — THE ARCHITECTURAL CORRECTION
-- ----------------------------------
-- The division in the `no <comparative>` frame is NOT "cancellation vs emphatic". It is:
--   * CONTINUATIVE / CANCELLING — "no longer", "no more". The negator cancels the EVENT.
--     A CLOSED, two-member set.
--   * COMPARATIVE-SCOPE — every OTHER negated comparative: "no earlier", "no harder", "no faster",
--     "no fewer", "no higher", "no cheaper", "no farther", "no smaller", "no louder", "no slower"…
--     The negator scopes over the COMPARISON and the event is ASSERTED. This class is OPEN and
--     PRODUCTIVE: Wiktionary's adverb sense of `no` glosses the frame generically ("before
--     comparatives with more and less, and idiomatically before other comparatives") with no member
--     list, because there is none to have.
--
-- Five successive rounds enumerated the OPEN side — a comparative Degree feature, a subject-aux
-- inversion test, a `than`-child test, a `than`-in-subtree test, then a correlative_adverb class,
-- then an emphatic_adverb class — and every one shipped a stored falsehood, because a productive
-- class cannot be finished. Seeding the CLOSED side is complete BY CONSTRUCTION, and it flips the
-- fail-safe to the safe direction: an unknown negated comparative now defaults to
-- NOT-a-cancellation (event asserted) instead of defaulting to a stored denial.
--
-- MEASURED on a two-sided corpus (13 cancellation forms — mid, fronted, trailing, 3rd-person,
-- multi-clause with unrelated comparative clauses, NP-internal `than` — and 24 comparative-scope
-- forms): previous arrangement 28/37, this one 37/37, with ZERO cancellations lost.
--
-- MEMBERS ARE LEMMAS, measured PER TAG (the lesson from "worse", whose lemma is tag-dependent):
--     surface "longer" -> lemma `long`  (RB and RBR alike)
--     surface "more"   -> lemma `more`  (JJR and RBR alike)
--
-- ⚠️ EMPTY IS THE CATASTROPHIC MODE. This class ADMITS rather than suppresses, so deactivating its
-- members in a tenant would silently stop EVERY cancellation from registering — the engine would
-- quietly re-affirm habits the user had cancelled, with no error. The consumer therefore treats an
-- empty resolution as a fault, falls back to the in-code floor, and logs loudly. The inverse risk is
-- bounded: adding a wrong lemma mis-reads only that lemma.
--
-- Consumed by linguistics._predicate_negated via
-- linguistic_cue_overlay.resolve_continuative_adverbs().

-- ============================================================================
-- Part 1: public template — seed the closed set
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('long', 'continuative_adverb',
   'Continuative/cancelling comparative: "no longer" cancels the event. Lemma of the surface '
   '"longer" (RB and RBR alike).',
   'I no longer drink coffee', 'seed_continuative_adverb', 0.90),
  ('more', 'continuative_adverb',
   'Continuative/cancelling comparative: "no more" cancels the event. Lemma of the surface "more" '
   '(JJR and RBR alike).',
   'No more do I drink coffee', 'seed_continuative_adverb', 0.90)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: retire the classes this one subsumes
-- ============================================================================
-- `correlative_adverb` (migration 267) and `emphatic_adverb` were both enumerations of the OPEN
-- side and are now dead: a non-continuative lemma is rejected by the admission test regardless of
-- membership. Deactivated rather than deleted so the rows remain visible as history; the resolvers
-- and consumers are removed in the same change, so nothing reads them either way.
-- `emphatic_adverb` is included defensively — its migration was never committed, but a tenant where
-- it was applied by hand would otherwise keep dead rows.
UPDATE public.linguistic_cues
   SET is_active = false
 WHERE category IN ('correlative_adverb', 'emphatic_adverb');

-- ============================================================================
-- Part 3: Per-user schemas (loop over faultline_* schemas)
-- ============================================================================
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
            WHERE category = 'continuative_adverb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        EXECUTE format($retire$
            UPDATE %I.linguistic_cues
               SET is_active = false
             WHERE category IN ('correlative_adverb', 'emphatic_adverb')
        $retire$, _schema);

        RAISE NOTICE 'Migration 269: continuative_adverb seeded, superseded classes retired in %',
                     _schema;
    END LOOP;
END $$;
