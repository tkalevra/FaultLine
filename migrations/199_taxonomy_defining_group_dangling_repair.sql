-- Migration 199: repair DANGLING rel_type references in entity_taxonomies defining groups,
--                and stop the growth engine writing them INTO the `public` template.
-- Date: 2026-07-31
--
-- WHAT WAS MEASURED (pre-prod `faultline_test`, 2026-07-31)
-- ---------------------------------------------------------
--   public.entity_taxonomies names 59 distinct rel_types in rel_types_defining_group.
--   48 of those 59 DO NOT EXIST in public.rel_types.
--
--   A freshly-migrated database (local, migrations only, no traffic) has exactly 11:
--       family          {parent_of,spouse,sibling_of,child_of}
--       household       {has_pet,member_of}
--       work            {works_for,managed_by,part_of,leads}
--       location        {located_at}
--       computer_system {part_of}
--       body_parts      {part_of}
--       pets            {has_pet}
--       animal          {}
--
--   So the 48 extra names were written into the TEMPLATE at RUNTIME by the ontology-growth
--   taxonomy-append path (`ingest.taxonomy_rel_type_appended` src/api/main.py, and
--   `re_embedder.taxonomy_rel_type_appended` src/re_embedder/embedder.py) while a `public`
--   search_path was bound. They are one tenant's shell/coffee-machine domain verbs
--   (pipes_to, sets_option, handles_signal, grinds, tamps, mounts, traps, …).
--   `public.rel_types` itself is CLEAN (zero engine_generated rows) — only the taxonomy
--   arrays leaked, which is why this went unnoticed.
--
-- WHY IT MATTERS — this is a PRECISION defect, not a recall one
-- -------------------------------------------------------------
--   `entity_taxonomies.rel_types_defining_group` is the taxonomy PROJECTION KEY: a scoped
--   query that resolves a taxonomy admits exactly that array into QueryPath.allowed_rels.
--   `src/provisioning/schema_manager.py` copies public.entity_taxonomies VERBATIM into every
--   new tenant, so every tenant provisioned since the leak inherits the foreign array.
--
--   Measured consequence on the pre-prod TEST seat (read-only probe, "tell me about my work"):
--       determine_path … taxonomy_groups=['work'] relationship_rels=[
--           'executes','configures','resides_in','join','represents','belongs_to','works_for',
--           'implements','runs_on','part_of','sign_for','references','uses']
--   The pristine seed would admit works_for/managed_by/part_of/leads (+ that tenant's OWN
--   grown join/sign_for). Eight of those thirteen rels are another tenant's vocabulary.
--   `uses` and `references` are the two most common verbs the LLM relation extractor mints,
--   so the moment ANY tenant grows `uses`, it silently becomes a member of work AND location
--   AND body_parts AND computer_system at once — four taxonomies collapse into one blurred
--   scope. That is precisely the "intent-less muddying" the ontology contract forbids, and it
--   is the OPPOSITE failure from an orphaned rel: it makes narrow queries WIDE.
--
-- WHY DANGLING NAMES ARE JUNK, NOT OPEN-WORLD REFERENCES
-- ------------------------------------------------------
--   RDF Schema is open-world and explicitly permits references to terms defined elsewhere
--   ("RDF vocabularies can describe relationships between vocabulary items from multiple
--   independently developed vocabularies" — W3C RDF Schema 1.1, §1.2), so a dangling IRI is
--   not per se invalid IN RDF. But `rel_types_defining_group` is not an open-world assertion:
--   it is a CLOSED-WORLD ENUMERATION used as a retrieval gate — the direct analogue of
--   `skos:Collection` / `skos:member`, where a Collection is a labelled, *enumerated* grouping
--   and is `disjoint with skos:Concept` (W3C SKOS Reference §9). A member that resolves to no
--   defined term contributes nothing to the enumeration and can only create false membership
--   later, when some tenant happens to mint that exact name. Wikidata's basic-membership
--   guidance makes the same separation from the other side: classification (P31/P279) must not
--   be mixed with membership/composition (P361/P463) — a grouping's membership predicate set is
--   a deliberate, closed choice, not an accident of vocabulary overlap.
--   Prior art in this repo: migration 070 Part C removed `reports_to` and `has_component` from
--   defining groups for exactly this reason ("rel_type doesn't exist in rel_types"). This
--   migration generalises that one-off, deterministically.
--
-- WHAT THIS MIGRATION DOES
-- ------------------------
--   PART A (data repair, `public` + every `faultline_%` schema). For each element of every
--     rel_types_defining_group, resolved AGAINST THAT SCHEMA'S OWN rel_types:
--       1. element IS a rel_type in this schema → KEEP verbatim
--       2. else                                 → DROP
--     Deterministic: exact set membership, nothing else. No cosine, no ILIKE, no substring,
--     no LLM, no morphology. Subject-agnostic: names no rel_type, no taxonomy, no domain —
--     every decision is read from metadata.
--     AUTHORITY ORDER (user > seed > growth) is respected: a rel the tenant legitimately GREW
--     exists in that tenant's rel_types, so rule 1 keeps it. Only names that resolve to nothing
--     are removed — i.e. only growth's write into a place it had no authority to write.
--
--     REJECTED ALTERNATIVE, recorded because it is the tempting one: also CANONICALISE a
--     dangling name through the curated `rel_type_aliases` table (belongs_to → member_of,
--     resides_in → lives_in are the only two of the 48 that have a canonical). Built, run, and
--     backed out — it MUDDIES. Compared against the pristine seed, `body_parts` is {part_of}
--     and `family` is {parent_of,spouse,sibling_of,child_of}; `belongs_to`/`resides_in` are
--     NOT seed intent in either, they are part of the same growth spill. Canonicalising them
--     LAUNDERS a dead pollution entry into a LIVE one — it would have added lives_in+member_of
--     to `body_parts` and widened `family` beyond what the seed ever declared. A dangling name
--     is junk; the correct repair is to delete it, never to make it work.
--
--     This repair is precision-restoring and retrieval-NEUTRAL today: a name that resolves to
--     no rel_type can never match a stored fact, so removing it cannot drop an answer. What it
--     removes is a LOADED GUN — the moment any tenant grows a rel that happens to share one of
--     those 48 common verb names, that tenant silently joins several taxonomies at once.
--
--     The eligibility trigger is suspended for the duration so this repair applies ITS OWN
--     policy only, and cannot silently invoke migration 122's cross-type arbitration on rows
--     it was not asked to re-arbitrate.
--
--   PART B (recurrence guard, `public` ONLY). A BEFORE INSERT OR UPDATE trigger on
--     public.entity_taxonomies that applies the SAME resolution and prunes what does not
--     resolve, with a loud RAISE WARNING naming the rel and the taxonomy. `public` is a
--     read-only template that NOTHING is supposed to grow into, so this constrains no
--     legitimate path — it makes the illegal one structurally impossible and audible,
--     whatever binds the search_path. Tenant schemas are deliberately NOT given this trigger:
--     a tenant DOES legitimately grow rels into its own taxonomies, and the ingest path can
--     append inside the same transaction that mints the rel.
--     Order note for future migrations: INSERT the rel_types row BEFORE naming it in a
--     defining group (the order migration 070 already uses).
--
-- IDEMPOTENT: re-running is a no-op once every name resolves.

-- ─────────────────────────────────────────────────────────────────────────────────────────
-- PART A — data repair, public + every tenant schema
-- ─────────────────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT 'public'
        UNION ALL
        SELECT schema_name
          FROM information_schema.schemata
         WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip a schema that does not carry the tables this repair reads.
        IF to_regclass(format('%I.entity_taxonomies', _schema)) IS NULL
           OR to_regclass(format('%I.rel_types', _schema)) IS NULL THEN
            CONTINUE;
        END IF;

        EXECUTE format('SET search_path TO %I', _schema);

        -- Apply ONLY this repair's policy (see header): suspend user triggers on the table
        -- so migration 122's eligibility arbitration is not re-run over untouched rows.
        BEGIN
            EXECUTE 'ALTER TABLE entity_taxonomies DISABLE TRIGGER USER';
        EXCEPTION WHEN OTHERS THEN
            NULL;   -- no user triggers / insufficient rights → proceed unguarded
        END;

        -- KEEP-OR-DROP, resolved against THIS schema's own rel registry. Nothing else.
        EXECUTE $sql$
            UPDATE entity_taxonomies t
               SET rel_types_defining_group = COALESCE((
                       SELECT array_agg(DISTINCT x.r ORDER BY x.r)
                         FROM (
                             SELECT (SELECT rt.rel_type FROM rel_types rt
                                      WHERE rt.rel_type = lower(e)) AS r
                               FROM unnest(t.rel_types_defining_group) AS e
                         ) x
                        WHERE x.r IS NOT NULL
                   ), '{}'::TEXT[])
             WHERE t.rel_types_defining_group IS NOT NULL
               AND array_length(t.rel_types_defining_group, 1) IS NOT NULL
        $sql$;

        BEGIN
            EXECUTE 'ALTER TABLE entity_taxonomies ENABLE TRIGGER USER';
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END LOOP;

    EXECUTE 'SET search_path TO public';
END $$;

-- ─────────────────────────────────────────────────────────────────────────────────────────
-- PART B — recurrence guard on the TEMPLATE only
-- ─────────────────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.enforce_public_template_defining_group()
RETURNS TRIGGER AS $body$
DECLARE
    _rel        TEXT;
    _kept       TEXT[] := '{}';
    _resolved   TEXT;
BEGIN
    IF NEW.rel_types_defining_group IS NULL
       OR array_length(NEW.rel_types_defining_group, 1) IS NULL THEN
        RETURN NEW;
    END IF;

    FOREACH _rel IN ARRAY NEW.rel_types_defining_group LOOP
        -- resolves against the template's own rel registry, or it does not belong here
        SELECT rt.rel_type INTO _resolved
          FROM public.rel_types rt
         WHERE rt.rel_type = lower(_rel);

        IF _resolved IS NULL THEN
            RAISE WARNING 'faultline: refused dangling rel % in defining group of PUBLIC template taxonomy % (public is a seed template; growth belongs in the tenant schema)',
                          _rel, NEW.taxonomy_name;
            CONTINUE;
        END IF;

        IF NOT (_kept @> ARRAY[_resolved]::TEXT[]) THEN
            _kept := array_append(_kept, _resolved);
        END IF;
    END LOOP;

    NEW.rel_types_defining_group := _kept;
    RETURN NEW;
END;
$body$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_public_template_defining_group_ins ON public.entity_taxonomies;
CREATE TRIGGER enforce_public_template_defining_group_ins
    BEFORE INSERT ON public.entity_taxonomies
    FOR EACH ROW EXECUTE FUNCTION public.enforce_public_template_defining_group();

DROP TRIGGER IF EXISTS enforce_public_template_defining_group_upd ON public.entity_taxonomies;
CREATE TRIGGER enforce_public_template_defining_group_upd
    BEFORE UPDATE ON public.entity_taxonomies
    FOR EACH ROW EXECUTE FUNCTION public.enforce_public_template_defining_group();
