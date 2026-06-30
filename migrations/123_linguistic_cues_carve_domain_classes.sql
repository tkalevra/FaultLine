-- Migration 120: linguistic_cues — CARVE OUT the DOMAIN-FLAVORED cue classes from the seed.
-- Date: 2026-06-29
--
-- WHY (lean-seed carve-out)
-- -------------------------
-- We over-seeded DOMAIN vocabulary as "cue classes" in linguistic_cues instead of GROWING it per
-- tenant. The seed must be the MINIMAL zero-shot grounding framework; the buildout is grown PER-TENANT
-- (public is a read-only template you seed FROM, never INTO). Three classes are domain-flavored, not
-- grammar/unit primitives, and are CARVED OUT here so a tenant grows its own from observation:
--
--   * social_role  (friend/colleague/roommate/classmate/boss/manager/neighbour/acquaintance/coworker)
--   * problem_noun (issue/problem/trouble/fault/difficulty/glitch/bug/error/concern)
--   * thin_type    (system/device/gadget/appliance/machine → device)
--
-- KEPT SEEDED (universal — do NOT touch): kinship_noun + kinship_gender (the near-universal human
-- primitive + the mental-health north-star) and the grammatical/unit primitives naming_verb /
-- possession_verb / acquisition_verb / aspectual_control_verb / inchoative_verb / lvc_support_verb /
-- svo_particle / relational_noun / unit_scalar. These are grammar/units, NOT domain.
--
-- WHAT REPLACES THE SEED (paired, non-negotiable):
--   1. FAIL-SAFE DEGRADE: a cold tenant that has not grown a carved class yet degrades the construction
--      to a generic but WALKABLE rel — "my colleague Sam" → (sam, related_to, user) — and QUEUES the
--      role for growth; NEVER dropped, NEVER an error. (linguistics.py / main.py.)
--   2. GROW from observation, freq-gated, per-tenant, deterministic: the ingest/harvest seam records a
--      candidate into ontology_evaluations (extraction_method='linguistic_cue_candidate'); the
--      re_embedder grow_linguistic_cue_candidates() promotes it (occurrence_count >= 3) into
--      <tenant>.linguistic_cues. Convergence-by-identity (UNIQUE (cue,category)), NO cosine.
--
-- thin_type active growth is DEFERRED (its only candidate signal is circular with GLiNER2's live
-- typing); its empty map is LOSSLESS because GLiNER2 supplies the real type at runtime.
--
-- THIS MIGRATION:
--   Part 1 — DELETE the three categories from public.linguistic_cues (the SEED SOURCE) so NO new tenant
--            inherits them (schema_manager.py also excludes them as defense-in-depth).
--   Part 2 — DELETE the three categories from EXISTING per-tenant schemas so they re-grow from a clean
--            slate. Only SEED-sourced rows are removed (source LIKE 'seed_%') so any already-GROWN
--            tenant rows (source='grown') are preserved.
-- Idempotent. KINSHIP / grammar / unit classes are never touched.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- ============================================================================
-- Part 1: public (SEED SOURCE / TEMPLATE) — remove the three carved categories
-- ============================================================================
-- Remove the seeded rows only (a grown public row should never exist — growth never writes public —
-- but guard with the seed-source filter anyway). After this, the blanket provisioning copy yields none.
DELETE FROM public.linguistic_cues
 WHERE category IN ('social_role', 'problem_noun', 'thin_type')
   AND (source LIKE 'seed_%' OR source IS NULL);

-- ============================================================================
-- Part 2: existing per-user schemas — remove the three carved categories (seed rows only)
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
        -- Only proceed if the table exists in this tenant (defensive).
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'linguistic_cues'
        ) THEN
            EXECUTE format($del$
                DELETE FROM %I.linguistic_cues
                 WHERE category IN ('social_role', 'problem_noun', 'thin_type')
                   AND (source LIKE 'seed_%%' OR source IS NULL)
            $del$, _schema);
            RAISE NOTICE 'Migration 120: carved social_role/problem_noun/thin_type seed rows removed from %', _schema;
        END IF;
    END LOOP;
END $$;
