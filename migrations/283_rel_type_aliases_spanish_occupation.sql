-- Migration 283: Spanish (es) query aliases for the occupation rel.
--
-- The employment chain files "Yo trabajo como ingeniero" as (user, occupation, ingeniero), but a
-- Spanish question about it ("¿Cuál es mi profesión?") scoped nothing: rel_type_aliases carries
-- the English aspect nouns (job, career, profession -> occupation) and no Spanish ones, so the
-- walk abstained on a value it had stored. Same rail as 218's vivir/trabajar forms: closed
-- Spanish nouns naming the aspect, seeded into the alias table (growable per tenant), no code
-- word list. 'trabajo' is NOT seeded here — 218 already maps it to works_for (it is also the
-- 1sg verb form); 'empleo' is left out because it names the employment (works_for) as often as
-- the occupation.
--
-- Idempotent: ON CONFLICT (alias) DO NOTHING (rel_type_aliases is UNIQUE on alias).

INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence)
VALUES
  ('occupation', 'profesión', 'es_seed', 0.95),
  ('occupation', 'profesion', 'es_seed', 0.90),
  ('occupation', 'ocupación', 'es_seed', 0.95),
  ('occupation', 'ocupacion', 'es_seed', 0.90),
  ('occupation', 'oficio',    'es_seed', 0.90)
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
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = _schema AND table_name = 'rel_type_aliases') THEN
            EXECUTE format($seed$
                INSERT INTO %I.rel_type_aliases (canonical_rel_type, alias, source, confidence)
                SELECT canonical_rel_type, alias, source, confidence
                FROM public.rel_type_aliases
                WHERE canonical_rel_type = 'occupation' AND source = 'es_seed'
                ON CONFLICT (alias) DO NOTHING
            $seed$, _schema);
        END IF;
    END LOOP;
END $$;
