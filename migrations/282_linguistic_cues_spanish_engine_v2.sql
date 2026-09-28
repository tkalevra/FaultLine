-- Migration 282: Spanish (es) seeds for the cue classes and rel_types the engine-v2 corpus
-- (migrations 167-281) added in English only.
--
-- Same rail and same rules as migration 218: the CODE carries no Spanish word list; the Spanish
-- members of each lexical class live in the DATABASE (public template -> copied into every tenant
-- schema, grown per-tenant). Every row below is a class membership grounded in Spanish grammar,
-- not a transliteration of the English lemma, and every class seeded here has a LIVE Spanish
-- consumer on this branch (a row nothing on the es path reads would be a dead cue that overclaims
-- capture — the rule 218 applied to 'la semana que viene').
--
--   • naming_noun (migration 270) — the nominal head of a copular naming frame. Read by the
--     scheme-portable naming-slot test (UD "mi nombre es Carlos": nombre = nsubj of the PROPN
--     predicate, copula as a cop child) and the genitive-name chain. Spanish naming nouns:
--     nombre, apodo, sobrenombre, mote, alias, apellido, seudónimo (DLE s.vv.).
--   • social_role (migrations 117/272) — a person-to-person social tie. Read by the possessed
--     person-role ladder (_possessed_person_role_rel), which _chain_possessive consults once the
--     possessive is recognised (es: "mi"/"mis" = det with Poss=Yes, NGLE §18.1a). Without these
--     rows "mi amiga Ana" falls through to the ownership leg and files a PERSON as an owned object
--     (user, owns, amiga). amigo/amiga -> friend_of (the same tie English friend seeds); colega,
--     compañero/compañera, vecino/vecina, conocido/conocida -> knows (the weak social tie the
--     English colleague/classmate/neighbour/acquaintance rows seed). Gendered and plural forms are
--     seeded as 218 did for kinship nouns: the es lemmatizer does not reliably fold amiga->amigo.
--     ⚠️ 272 seeds the English loanword 'amigo' -> knows from WordNet (amigo.n.01). On this branch
--     'amigo' is the ordinary Spanish word for friend, so its SEED row (and only a seed row still
--     carrying 272's value) is re-pointed to friend_of below; a tenant-grown or corrected row is
--     never touched.
--   • dosage_noun (migration 280) — the measurement-family head nouns (dose/dosage/quantity/level/
--     amount -> canonical 'quantity'). Read language-neutrally by main.py's measurement-family
--     mint suppression and the registry's bare family-noun refusal (issue #22): a bare "nivel" /
--     "dosis" is an ATTRIBUTE name, never an entity. Spanish: dosis, dosificación, cantidad, nivel.
--     (The registry's head-FINAL '<substance> <family-noun>' compound redirect, issue #21, does not
--     match the head-INITIAL Spanish "dosis de vitamina D" — that redirect is inert on es; see
--     LEEME-es.md.)
--   • measure_noun (migration 281) — record/vocabulary -> the same canonical. Spanish: récord /
--     record (both spellings are standard, DLE), vocabulario.
--   • unit_scalar (migration 187 added 'second') — segundo/segundos -> duration, and the accented
--     plural 'días' that 218 missed (218 seeded 'día' and the unaccented 'dias' only). Read by the
--     es tener-measure chain's unit map ("tardó 90 segundos").
--
-- rel_types.natural_language: 190 (duration) and 215 (postal_code) seed English templates, and the
-- query walk matches a Spanish query's content words against this column (218's rationale — the
-- branch IS the language). Replaced only where the column still holds the English seed text, so a
-- tenant-grown template is never clobbered.
--
-- NOT seeded, deliberately (documented in LEEME-es.md): relocation_verb, locative_participle,
-- identifier_noun, position_noun, lvc_support_verb, natal_predicate, offspring_noun,
-- implicative_verb, exemplification_marker, continuative_adverb, measure_verb. Their consuming
-- chains read Penn-only structure (prep->pobj with English prepositions, npadvmod, auxpass,
-- "than"-standards, compound-final heads) that the UD es parse never produces, so a Spanish row
-- would be dead. They stay inert on es until those chains grow UD arms.
--
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING; guarded UPDATEs.

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('nombre',       'naming_noun', 'Nominal head of a copular naming frame: "el nombre de X es Y".', 'Mi nombre es Carlos.',            'seed_naming_noun_es', 0.95),
  ('apodo',        'naming_noun', 'Nominal head of a copular naming frame (informal label).',        'Su apodo es Chema.',              'seed_naming_noun_es', 0.90),
  ('sobrenombre',  'naming_noun', 'Nominal head of a copular naming frame (informal label).',        'Su sobrenombre es El Flaco.',     'seed_naming_noun_es', 0.85),
  ('mote',         'naming_noun', 'Nominal head of a copular naming frame (informal label).',        'Su mote es Pecas.',               'seed_naming_noun_es', 0.85),
  ('alias',        'naming_noun', 'Nominal head of a copular naming frame (alternate label).',       'Su alias es Rulo.',               'seed_naming_noun_es', 0.90),
  ('apellido',     'naming_noun', 'Nominal head of a copular naming frame (family name).',           'Mi apellido es Torres.',          'seed_naming_noun_es', 0.90),
  ('seudónimo',    'naming_noun', 'Nominal head of a copular naming frame (pen name).',              'Su seudónimo es Azorín.',         'seed_naming_noun_es', 0.85),
  ('seudonimo',    'naming_noun', 'Nominal head of a copular naming frame (pen name, no accent).',   'Su seudonimo es Azorin.',         'seed_naming_noun_es', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('amigo',      'social_role', 'friend_of', 'mi amigo Luis',        'seed_social_role_es', 0.90),
  ('amiga',      'social_role', 'friend_of', 'mi amiga Ana',         'seed_social_role_es', 0.90),
  ('amigos',     'social_role', 'friend_of', 'mis amigos',           'seed_social_role_es', 0.85),
  ('amigas',     'social_role', 'friend_of', 'mis amigas',           'seed_social_role_es', 0.85),
  ('colega',     'social_role', 'knows',     'mi colega Marta',      'seed_social_role_es', 0.85),
  ('colegas',    'social_role', 'knows',     'mis colegas',          'seed_social_role_es', 0.82),
  ('compañero',  'social_role', 'knows',     'mi compañero Iván',    'seed_social_role_es', 0.82),
  ('compañera',  'social_role', 'knows',     'mi compañera Sara',    'seed_social_role_es', 0.82),
  ('compañeros', 'social_role', 'knows',     'mis compañeros',       'seed_social_role_es', 0.80),
  ('compañeras', 'social_role', 'knows',     'mis compañeras',       'seed_social_role_es', 0.80),
  ('vecino',     'social_role', 'knows',     'mi vecino Paco',       'seed_social_role_es', 0.82),
  ('vecina',     'social_role', 'knows',     'mi vecina Lola',       'seed_social_role_es', 0.82),
  ('vecinos',    'social_role', 'knows',     'mis vecinos',          'seed_social_role_es', 0.80),
  ('vecinas',    'social_role', 'knows',     'mis vecinas',          'seed_social_role_es', 0.80),
  ('conocido',   'social_role', 'knows',     'un conocido',          'seed_social_role_es', 0.80),
  ('conocida',   'social_role', 'knows',     'una conocida',         'seed_social_role_es', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

-- 272's WordNet loanword row: re-point ONLY the untouched seed row.
UPDATE public.linguistic_cues
   SET description = 'friend_of', example_text = 'mi amigo Luis'
 WHERE cue = 'amigo' AND category = 'social_role'
   AND source = 'seed_social_role' AND description = 'knows';

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('dosis',          'dosage_noun',  'quantity',   'mi dosis de vitamina D es 1000 UI',  'seed_dosage_noun_es',  0.90),
  ('dosificación',   'dosage_noun',  'quantity',   'la dosificación es 5 ml',            'seed_dosage_noun_es',  0.85),
  ('dosificacion',   'dosage_noun',  'quantity',   'la dosificacion es 5 ml',            'seed_dosage_noun_es',  0.85),
  ('cantidad',       'dosage_noun',  'quantity',   'la cantidad es 200 mg',              'seed_dosage_noun_es',  0.85),
  ('nivel',          'dosage_noun',  'quantity',   'mi nivel de hierro es 40',           'seed_dosage_noun_es',  0.85),
  ('récord',         'measure_noun', 'record',     'mi récord es 42 km',                 'seed_measure_noun_es', 0.85),
  ('record',         'measure_noun', 'record',     'mi record es 42 km',                 'seed_measure_noun_es', 0.85),
  ('vocabulario',    'measure_noun', 'vocabulary', 'su vocabulario es de 300 palabras',  'seed_measure_noun_es', 0.85),
  ('segundo',        'unit_scalar',  'duration',   'tardó un segundo',                   'seed_unit_scalar',     0.80),
  ('segundos',       'unit_scalar',  'duration',   'tardó 90 segundos',                  'seed_unit_scalar',     0.80),
  ('días',           'unit_scalar',  'duration',   'tarda dos días',                     'seed_unit_scalar',     0.80)
ON CONFLICT (cue, category) DO NOTHING;

UPDATE public.rel_types SET natural_language = 'X tardó Y en completarse'
 WHERE rel_type = 'duration' AND natural_language = 'X took Y to complete';
UPDATE public.rel_types SET natural_language = 'X tiene el código postal Y'
 WHERE rel_type = 'postal_code' AND natural_language = 'X has the postal code Y';

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
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = _schema AND table_name = 'linguistic_cues') THEN
            EXECUTE format($seed$
                INSERT INTO %I.linguistic_cues
                    (cue, category, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     source, is_active, archived_at, last_matched_at)
                SELECT cue, category, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       source, is_active, archived_at, last_matched_at
                FROM public.linguistic_cues
                WHERE source IN ('seed_naming_noun_es', 'seed_social_role_es',
                                 'seed_dosage_noun_es', 'seed_measure_noun_es')
                   OR (category = 'unit_scalar' AND cue IN ('segundo', 'segundos', 'días'))
                ON CONFLICT (cue, category) DO NOTHING
            $seed$, _schema);
            EXECUTE format($upd$
                UPDATE %I.linguistic_cues
                   SET description = 'friend_of', example_text = 'mi amigo Luis'
                 WHERE cue = 'amigo' AND category = 'social_role'
                   AND source = 'seed_social_role' AND description = 'knows'
            $upd$, _schema);
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = _schema AND table_name = 'rel_types') THEN
            EXECUTE format($rt$
                UPDATE %I.rel_types SET natural_language = 'X tardó Y en completarse'
                 WHERE rel_type = 'duration' AND natural_language = 'X took Y to complete'
            $rt$, _schema);
            EXECUTE format($rt$
                UPDATE %I.rel_types SET natural_language = 'X tiene el código postal Y'
                 WHERE rel_type = 'postal_code' AND natural_language = 'X has the postal code Y'
            $rt$, _schema);
        END IF;
        RAISE NOTICE 'Migration 282: Spanish engine-v2 cue/rel seeds applied in %', _schema;
    END LOOP;
END $$;
