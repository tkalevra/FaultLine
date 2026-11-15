-- Migration 320: linguistic_cues — seed the BLENDED-FAMILY kinship cues (the blended-family
-- smoke, 2026-11-12).
--
-- WHY
-- ---
-- The owner's real family shape (current wife + ex-wife + stepson + 5 kids + 3 pets) exercises
-- kin titles the seed floor never carried. The possessive kin rail resolves the possessed head
-- through the per-tenant kinship_noun cue class; a title OUTSIDE the map falls to the generic
-- ownership leg and files a PERSON as an OWNED OBJECT — measured on the blended-family smoke
-- seat 9e61c3d8: "My ex-wife Isabelle and I split up in 2022" minted (user, owns, ex-wife),
-- so "what's my ex-wife's name?" could not resolve the referent while the current-wife query
-- had to stay unpoisoned by it. The ex- prefix is a DISTINCT-REFERENT modifier (the 289 class):
-- the ex-wife is never the current wife, so the ex titles carry their OWN rel — the wife/spouse
-- queries fan ONLY the spouse rel and the ex-wife query fans only ex_spouse.
--
-- ROWS (kinship_noun → rel_type, the map's convention: the rel runs FROM the named person TO
-- the possessor — "my son" → (son, child_of, user)):
--   ex-wife / ex wife / ex-husband / ex husband → ex_spouse   (the blended-family litmus (a))
--   stepson / step-son / stepdaughter / step-daughter → step_parent_of (the live seat already
--       reached step_parent_of through the LLM lane; the cue makes the deterministic rail agree)
--   eldest / youngest / oldest → child_of (superlative birth-order kin cues — "our eldest,
--       Priya" binds child_of AT the named child)
--
-- GENDER (kinship_gender): the gendered ex/step roles (the same convention as migration 117 —
-- ONLY the roles whose gender the language states; eldest/youngest/oldest stay absent).
--
-- DATA, NOT CODE: the overlay's bootstrap floor is untouched; these rows seed the class the
-- same way 109/117/120/289 did, and the class stays tenant-growable.

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('ex-wife', 'kinship_noun', 'ex_spouse',
   'My ex-wife Isabelle and I split up in 2022.', 'seed_blended_family_kinship', 0.95),
  ('ex wife', 'kinship_noun', 'ex_spouse',
   'My ex wife Isabelle and I split up in 2022.', 'seed_blended_family_kinship', 0.95),
  ('ex-husband', 'kinship_noun', 'ex_spouse',
   'My ex-husband Marco and I split up in 2022.', 'seed_blended_family_kinship', 0.95),
  ('ex husband', 'kinship_noun', 'ex_spouse',
   'My ex husband Marco and I split up in 2022.', 'seed_blended_family_kinship', 0.95),
  ('stepson', 'kinship_noun', 'step_parent_of',
   'My stepson Rowan is 12.', 'seed_blended_family_kinship', 0.95),
  ('step-son', 'kinship_noun', 'step_parent_of',
   'My step-son Rowan is 12.', 'seed_blended_family_kinship', 0.95),
  ('stepdaughter', 'kinship_noun', 'step_parent_of',
   'My stepdaughter Ailsa is 9.', 'seed_blended_family_kinship', 0.95),
  ('step-daughter', 'kinship_noun', 'step_parent_of',
   'My step-daughter Ailsa is 9.', 'seed_blended_family_kinship', 0.95),
  ('eldest', 'kinship_noun', 'child_of',
   'Our eldest, Priya, turned 14 on January 22.', 'seed_blended_family_kinship', 0.90),
  ('youngest', 'kinship_noun', 'child_of',
   'Our youngest Saskia is 3.', 'seed_blended_family_kinship', 0.90),
  ('oldest', 'kinship_noun', 'child_of',
   'Our oldest, Priya, turned 14 on January 22.', 'seed_blended_family_kinship', 0.90),
  ('ex-wife', 'kinship_gender', 'female',
   'My ex-wife Isabelle and I split up in 2022.', 'seed_blended_family_kinship', 0.90),
  ('ex-husband', 'kinship_gender', 'male',
   'My ex-husband Marco and I split up in 2022.', 'seed_blended_family_kinship', 0.90),
  ('stepson', 'kinship_gender', 'male',
   'My stepson Rowan is 12.', 'seed_blended_family_kinship', 0.90),
  ('stepdaughter', 'kinship_gender', 'female',
   'My stepdaughter Ailsa is 9.', 'seed_blended_family_kinship', 0.90)
ON CONFLICT (cue, category) DO NOTHING;

-- Part 2: THE BIRTHDAY ALIAS — the recall walk's scalar terminal reads the attribute by
-- the QUERY noun; the date-attribute capture files the birth-date attribute by its
-- PARTICIPILE stem (born_on). "when is my wife's birthday?" → the terminal noun
-- "birthday" never matched the stored born_on rows (the blended-family smoke measured
-- the spouse prose render with the birth-date row live but unvoiced). One guarded alias
-- row bridges the noun to the canonical scalar rel (UNIQUE(alias) — one canonical per
-- alias; the row lands only when born_on exists in rel_types).
INSERT INTO public.rel_type_aliases (alias, canonical_rel_type)
SELECT 'birthday', 'born_on'
WHERE EXISTS (SELECT 1 FROM public.rel_types WHERE rel_type = 'born_on')
ON CONFLICT (alias) DO NOTHING;

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
            WHERE category IN ('kinship_noun', 'kinship_gender')
              AND source = 'seed_blended_family_kinship'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);
    END LOOP;
END $$;
