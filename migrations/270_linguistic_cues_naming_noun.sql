-- Migration 270: linguistic_cues — seed the NAMING-NOUN class ('naming_noun').
-- Date: 2026-08-25
--
-- WHY
-- ---
-- The speaker-rename refusal (NAME_CORRECTION_SPEAKER_GUARD → linguistics.
-- naming_frame_third_party_profile → main.correct_fact) recognises a copular NAMING frame in order
-- to answer one question: does this turn say the name belongs to somebody OTHER than the speaker?
-- Until now the naming noun in that frame was the in-code literal lemma "name", so
--
--     "My sister's NICKNAME is Thorne, not Wrenna."
--
-- was not a naming frame at all, the guard had no opinion, and the correction endpoint's LLM subject
-- guess was free to write `thorne` onto the SPEAKER'S own entity. Measured on fresh tenants, the
-- sibling realizations of the same defect wrote a third party's name onto the user 2 times in 10
-- identical runs (the corruption is nondeterministic — it rides the LLM's subject guess).
--
-- An in-code lemma is also the wrong RAIL: every other lexical class the deriver consults lives in
-- linguistic_cues and grows per-tenant. This puts naming nouns on the same rail.
--
-- WHY NOT REUSE 'naming_verb'
-- ---------------------------
-- The two classes overlap in surface form (name/nickname/title/label/term are both verbs and nouns)
-- but not in membership. `alias` and `surname` are naming NOUNS with no verbal use in this frame;
-- `call`/`dub`/`christen`/`entitle` are naming VERBS whose noun readings are not naming nouns at all
-- ("my sister's call"). Reusing the verb set would be both over- and under-inclusive in a class that
-- decides whether one real person's name may be written onto another.
--
-- MEMBERSHIP BIAS IS SET BY THE CONSUMER'S ASYMMETRIC FAILURE MODE
-- ----------------------------------------------------------------
-- A member that should not be here costs at most a CLARIFICATION QUESTION on a correction that was
-- going to land on the speaker anyway. A member MISSING costs a third party's name written onto the
-- speaker's own entity — unrecoverable corruption of user truth. So seed inclusively within the
-- evidenced class; the class is a bounded lexical field (the nominal exponents of the naming
-- relation), not an open productive one.
--
-- ⚠️ EMPTY IS THE CATASTROPHIC MODE. Deactivating every member in a tenant silently switches the
-- refusal OFF for the copular realizations, with no error anywhere. The resolver
-- (linguistic_cue_overlay.resolve_naming_nouns) therefore treats an empty resolution as a fault and
-- falls back to the in-code floor in linguistics._NAMING_NOUN_LEMMAS.
--
-- Consumed by linguistics.naming_frame_third_party_profile via
-- linguistic_cue_overlay.resolve_naming_nouns().

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('name',     'naming_noun', 'Nominal head of a copular naming frame: "<bearer>''s name is X".',
   'My sister''s name is Thorne, not Wrenna.',  'seed_naming_noun', 0.95),
  ('nickname', 'naming_noun', 'Nominal head of a copular naming frame (informal label).',
   'Her nickname is Thorne, not Wrenna.',       'seed_naming_noun', 0.90),
  ('alias',    'naming_noun', 'Nominal head of a copular naming frame (alternate label).',
   'His alias is Thorne.',                      'seed_naming_noun', 0.90),
  ('moniker',  'naming_noun', 'Nominal head of a copular naming frame (informal label).',
   'Her moniker is Thorne.',                    'seed_naming_noun', 0.85),
  ('surname',  'naming_noun', 'Nominal head of a copular naming frame (family name).',
   'My sister''s surname is Thorne, not Wrenna.', 'seed_naming_noun', 0.90),
  ('byname',   'naming_noun', 'Nominal head of a copular naming frame (secondary name).',
   'His byname is Thorne.',                     'seed_naming_noun', 0.80),
  ('epithet',  'naming_noun', 'Nominal head of a copular naming frame (descriptive name).',
   'Her epithet is Thorne.',                    'seed_naming_noun', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas)
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
            WHERE category = 'naming_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 270: naming_noun seeded in %', _schema;
    END LOOP;
END $$;
