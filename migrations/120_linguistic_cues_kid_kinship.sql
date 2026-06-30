-- 120_linguistic_cues_kid_kinship.sql
-- Add the colloquial kinship noun "kid" to the kinship_noun cue class.
--
-- WHY: the collective member-list reconciliation (and the existing _kin_collective pre-pass) routes a
-- named-member enumeration by the HEAD noun's kinship cue ("we have three kids: Mia, Theo, Leo"
-- → each member child_of the user). The seed kinship_noun class carried "child" but NOT its everyday
-- synonym "kid", so "kids" fell through to the generic chains and was mangled into (user, owns, kid)
-- + (member, instance_of, "kids"). "kid" → child_of (same role "child" plays toward the possessor: the
-- kid is the CHILD of me). No gender (gender-neutral, like "child").
--
-- This is GROWTH of the DB-grown cue class (subject-agnostic, metadata-driven) — NOT an in-code
-- literal. Tenant schemas COPY public.linguistic_cues at provisioning (schema_manager.py), and the
-- per-tenant overlay reads public ∪ tenant, so seeding public reaches both new and existing tenants.

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('kid', 'kinship_noun', 'child_of', 'my kid''s school', 'seed_kinship', 0.88)
ON CONFLICT (cue, category) DO NOTHING;
