-- 244_alias_coreference_warrant.sql
--
-- ⚠️⚠️ DELIBERATELY **UNAPPLIED**. Written so the honest answer to a measured defect exists in
-- the repository, NOT because it is ready to run. It is not wired to any reader or writer, and
-- applying it alone changes nothing. Do not run it as part of a deploy. See
-- the internal design record §9.6 for the decision and what has to land with it.
--
-- ── THE DEFECT THIS EXISTS FOR ────────────────────────────────────────────────────────────
-- The design's central move is that the CO-REFERENCE WARRANT is an axis ORTHOGONAL to trust:
-- rank answers "how far do I trust this source to pick the display name?", the warrant answers
-- "was this co-reference claim licensed by anything at all?", and collapsing them into one
-- number is what made the weld guard's third arm inexpressible in the first place
-- (src/entity_registry/registry.py, `_WARRANTED_SOURCES`).
--
-- But the warrant is PERSISTED IN `entity_aliases.preference_source` — a single column that
-- `EntityRegistry.register_alias` arbitrates BY RANK ("ratchet preference_source UP only").
-- Since rank('lexical') = 3 < rank('rel_default') = 4, the orthogonal axis is being stored in a
-- slot governed by the axis it is supposed to be orthogonal TO. Measured against a throwaway
-- Postgres, driving the real `register_alias`:
--
--     writes ['lexical']                  -> stored 'lexical'     warranted=True
--     writes ['rel_default','lexical']    -> stored 'rel_default'  warranted=False   <- never persists
--     writes ['lexical','rel_default']    -> stored 'rel_default'  warranted=False   <- erased later
--
-- So a granted warrant is not durable: it is lost if the alias row already exists at
-- `rel_default`, and an ordinary later re-ingest at `rel_default` erases one that did land.
--
-- The FAILURE DIRECTION IS SAFE — a reader that has lost the warrant sees "unwarranted", which
-- is strictly stricter (the weld guard refuses more, never less). That is why this is a
-- correctness-of-design defect rather than a corruption risk, and why it is recorded and left
-- unapplied rather than rushed in beside a behaviour change.
--
-- ── WHAT THIS WOULD DO ────────────────────────────────────────────────────────────────────
-- Give the warrant its own column so it is never rank-arbitrated. The column is a nullable
-- TEXT naming the AUTHORITY that licensed the co-reference (not a boolean): the point of the
-- axis is that a warranted writer can NAME its licence, and a boolean throws that away. This
-- follows the W3C PROV-O shape the design already matches by accident — `prov:wasDerivedFrom`
-- / `prov:qualifiedDerivation`, i.e. the justification recorded ALONGSIDE the value rather than
-- folded into it.
--
-- ⚠️ APPLYING THIS ALONE IS INERT AND WOULD BE MISLEADING. All of the following must land with
-- it, and none of it is written yet:
--   1. `register_alias` must write the column, and must merge it by UNION (a warrant once
--      recorded is never withdrawn by a later unwarranted write) — NOT by rank. That merge rule
--      is the whole point; a max-by-rank merge here would reproduce the defect in a new column.
--   2. `weld_guard._warranted` must read the column instead of `preference_source`, and
--      `weld_guard.weld_verdict`'s ARM-3 band check must keep reading `preference_source` for
--      RANK — the two reads must not be re-collapsed.
--   3. `registry._WARRANTED_SOURCES` becomes a BACKFILL RULE for legacy rows rather than the
--      live definition, and the `lexical` provenance token can then rank wherever trust says it
--      should, independently of its licence.
--   4. A backfill for existing rows, and the per-tenant template
--      (`src/provisioning/templates/user_schema.sql`) updated so NEW tenants get the column —
--      a migration that does not also change the template silently splits the schema shape
--      between old and new tenants.
--   5. Mutation coverage proving the new column has teeth (the internal design record).
--
-- ── PER-TENANT FANOUT ─────────────────────────────────────────────────────────────────────
-- ⚠️ CORRECTED. An earlier revision of this file carried the sentence "Per-tenant: this runs
-- against EVERY `faultline_*` schema, not `public`" above a BARE, UNQUALIFIED
-- `ALTER TABLE entity_aliases ...` — no `search_path`, no schema qualification, no loop. The
-- header asserted a fanout the SQL did not perform. Run as written, with the default
-- `search_path` of `"$user", public`, it would have altered `public.entity_aliases` — which
-- EXISTS on production as the LEGACY table — and ZERO tenant schemas: a silent no-op for the
-- entire data plane, while the comment told the reader the fanout was handled. That is the
-- doc-vs-code drift class this repository keeps paying for, so the SQL now does what the
-- sentence says.
--
-- The shape below is the repository's OWN convention for this exact operation, matched rather
-- than reinvented — see `migrations/214_entity_alias_display_form.sql` (the nearest analogue:
-- same table, same `DO $$ ... EXECUTE format('... %I.entity_aliases ...')` loop, the same
-- escaped `LIKE 'faultline\_%'`, and the same skip for a schema with no `entity_aliases` from a
-- partial/aborted provisioning), `migrations/216_icu_collation_for_display_text.sql`, and
-- `migrations/192_entities_node_role.sql`.
--
-- `public` is DELIBERATELY NOT TOUCHED. Unlike migration 192 — which stamps both because
-- `public.entities` is a live seed template — `public.entity_aliases` is the LEGACY shape (it
-- still carries a `user_id` column that no per-tenant table has), it is never read at runtime
-- by ingest or query, and the weld guard only ever runs bound to a tenant `search_path`. Adding
-- the column there would widen the legacy/tenant divergence for no reader.
--
-- ⚠️ NEW TENANTS ARE STILL NOT COVERED. This alters EXISTING schemas only; a tenant provisioned
-- after it runs is built from `src/provisioning/templates/user_schema.sql`, which does NOT yet
-- declare this column (verified: absent). That is item 4 above and it is still unwritten — a
-- migration that does not also change the template silently splits the schema shape between old
-- and new tenants. Both must land together.
--
-- Idempotent throughout (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`), so it is
-- safe to re-run — which the per-schema fanout tax makes a practical requirement, not a nicety.
--
-- ⚠️ NO `user_id` PREDICATE ANYWHERE, deliberately: the bound per-tenant `search_path` IS the
-- scope. Per-tenant tables have no `user_id` column (only `entity_attributes` does), so such a
-- predicate is an `UndefinedColumn` that ABORTS THE CALLER'S TRANSACTION and surfaces as a
-- failure somewhere else entirely. See CLAUDE.md, "Database Schema".

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip a schema that has no entity_aliases table (partial/aborted provisioning).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'entity_aliases'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.entity_aliases '
            'ADD COLUMN IF NOT EXISTS coreference_warrant TEXT',
            _schema
        );

        EXECUTE format(
            'COMMENT ON COLUMN %I.entity_aliases.coreference_warrant IS %L',
            _schema,
            'The AUTHORITY that licensed this surface to co-refer with the entity''s other '
            'labels (e.g. ''wordnet_synset'', ''abbreviation_definition''), or NULL for no '
            'recorded licence. ORTHOGONAL to preference_source: that column records TRUST and '
            'is arbitrated by rank, this one records JUSTIFICATION and must be merged by '
            'UNION, never by rank. Storing the warrant in preference_source is what made it '
            'non-durable — see migration 244.'
        );

        -- Partial index: warranted rows are the rare case and are the ones the guard asks about.
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_entity_aliases_coreference_warrant '
            'ON %I.entity_aliases (entity_id) WHERE coreference_warrant IS NOT NULL',
            _schema
        );

        RAISE NOTICE 'migration 244: coreference_warrant added to %.entity_aliases', _schema;
    END LOOP;
END $$;
