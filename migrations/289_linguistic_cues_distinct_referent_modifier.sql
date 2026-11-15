-- Migration 289: linguistic_cues — seed the DISTINCT-REFERENT MODIFIER class (#201).
-- Date: 2026-10-03
--
-- WHY
-- ---
-- A possessive / definite denial ("I don't have my old dog anymore.") presupposes ONE existing
-- referent, so with exactly one type match on the slot it names that match (#197). But an adjective
-- on the possessed head can instead mark a referent OTHER than the one on file: "my OTHER dog",
-- "my NEW dog", "my DIFFERENT car". Reading those as the one stored dog retired it — silent data
-- loss (#201). The parse cannot tell "old" (descriptive) from "other" (contrastive): both are
-- amod / JJ with the same morphology. Ordinals ("second", "third") ARE told apart by grammar
-- (spaCy ORDINAL), so they are not seeded here.
--
-- This class lists the modifier LEMMAS that introduce a distinct referent; the consumer retires
-- nothing when the possessed head carries one. Same signal #137 uses for naming
-- (`_speaker_possessed_mentions`: a modifier on the possessed noun may pick out another referent).
--
-- DATA, NOT CODE: the in-code floor is deliberately EMPTY. Defence in depth only: since #206 every
-- restricted denial retires its single match only when stored facts verify each modifier, so an
-- unavailable class costs nothing in safety. Tenants grow the class per seat.
--
-- Consumed by main._denied_np_reading via
-- linguistic_cue_overlay.resolve_distinct_referent_modifiers().

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('other', 'distinct_referent_modifier',
   'Contrastive modifier: the possessed head names a referent other than the one on file.',
   'I don''t have my other dog anymore', 'seed_distinct_referent_modifier', 0.90),
  ('another', 'distinct_referent_modifier',
   'Additive modifier: a further referent, not the one on file.',
   'I lost another of my cats', 'seed_distinct_referent_modifier', 0.90),
  ('new', 'distinct_referent_modifier',
   'Contrastive modifier: a newer referent, set against an older one on file.',
   'I don''t have my new car anymore', 'seed_distinct_referent_modifier', 0.80),
  ('different', 'distinct_referent_modifier',
   'Contrastive modifier: a referent distinct from the one on file.',
   'I don''t work at my different job anymore', 'seed_distinct_referent_modifier', 0.80),
  ('additional', 'distinct_referent_modifier',
   'Additive modifier: a further referent, not the one on file.',
   'I don''t have my additional phone anymore', 'seed_distinct_referent_modifier', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

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
            WHERE category = 'distinct_referent_modifier'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);
    END LOOP;
END $$;
