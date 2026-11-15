-- Migration 288: linguistic_cues — seed the UNIVERSAL-DENIAL DETERMINER class (#196).
-- Date: 2026-10-03
--
-- WHY
-- ---
-- A denial retires stored instances only when its object NP denies EVERY instance of the type:
-- NOT-EXISTS x (type(x) and rel(user, x)). The parse already tells us that for an indefinite article
-- (Definite=Ind: "I don't have a cat"), a bare noun and a plural ("I don't have cats"). It does NOT
-- tell us for a quantifier determiner: en_core_web_sm gives "any", "no", "another", "some", "every",
-- "all", "each", "both", "either" the SAME empty morphology. Reading every unmarked determiner as
-- the universal made "I don't have another cat." (= no ADDITIONAL cat; it presupposes the user's
-- cats) retire every cat — silent data loss (#196).
--
-- So the universal reading of an unmarked determiner is ADMITTED only for the members of this class:
-- the negative-polarity / negative determiners whose meaning under (or as) sentential negation is
-- the universal denial. Every other unmarked determiner ("another", "some", "every", "all", "each",
-- "both") FAILS SAFE in the consumer: an unidentified subset, nothing retired. That is the safe
-- direction: under-retirement is visible and recoverable; over-retirement is silent data loss.
--
-- DATA, NOT CODE: the in-code floor for this category is deliberately EMPTY, so a DB-down tenant
-- retires nothing on a quantifier denial (fail-safe). Tenants grow the class per seat.
--
-- MEMBERS ARE LEMMAS (spaCy lemma == surface for both, tag DT).
--
-- Consumed by main._denied_np_reading via
-- linguistic_cue_overlay.resolve_universal_denial_determiners().

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('any', 'universal_denial_determiner',
   'Negative-polarity determiner: under sentential negation it denies every instance of the type.',
   'I don''t have any cats anymore', 'seed_universal_denial_determiner', 0.90),
  ('no', 'universal_denial_determiner',
   'Negative determiner: it denies every instance of the type.',
   'I have no cats anymore', 'seed_universal_denial_determiner', 0.90)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: per-user schemas
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
        IF to_regclass(format('%I.linguistic_cues', _schema)) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category = 'universal_denial_determiner'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);
    END LOOP;
END $$;
