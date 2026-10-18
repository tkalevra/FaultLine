-- Migration 293: Spanish (es) seeds for the CORRECTION-EVIDENCE stores the FOSS-012
-- affirmative-statement decline reads.
--
-- FOSS-012: on the es zero-shot intent set, a plain first-person present-tense
-- affirmative statement ("Uso el puerto 9004 en el servidor principal.") scores the
-- CORRECTION label at gate-passing confidence; the decline in classify_intent demotes
-- such a turn to STATEMENT only when the turn carries ZERO correction-shape evidence.
-- One of those evidence screens is DB-GROWN (the house rail: the code carries no word
-- list; lexical class members live in the database, public template -> copied into
-- every tenant schema, grown per-tenant). The live read on the es eval stack
-- (fl-eval-es @ 74e03fd7, 2026-10-18) showed both stores below carry ENGLISH-ONLY
-- seeds, so an es turn with a REAL correction marker had no row the screen could read:
--
--   • public.correction_signals: 12 rows, all en (migration 032). Seeded here with the
--     es reformulation/contradiction/negation literals, each mirroring an en row:
--     actually -> en realidad; sorry -> perdón/disculpa; wait -> espera; i meant ->
--     quería decir; was wrong -> me equivoqué; my mistake -> mi error; not true/right
--     -> no es verdad / no es cierto; plus the contrastive reformulator en cambio.
--     Accented AND unaccented typings for the accent-ambiguous forms (the literal
--     screen normalizes punctuation, not accents — 218/282 precedent).
--   • public.linguistic_cues category cessative_verb: ZERO rows anywhere (the
--     en floor is in-code only), so the decline's cessation admission screen
--     (_cessative_shape_admitted, mirroring _spacy_cue_route) could never admit an es
--     lexically-expressed removal ("dejo de usar el puerto 9004") and the decline
--     would have demoted it. Seeded: dejar, abandonar, cesar — the es members of the
--     SIL cessative-aspect class the en floor implements (Freed 1979; NGLE §37.3 for
--     the deixar de + inf construction). Complement shapes ride the description column
--     (the same one-value-per-row keyed-map convention migrations 117/272 use).
--     PARAR is DELIBERATELY NOT seeded: it is the polysemous es "stop" (NGLE records
--     the locative "parar en casa") and the en floor excludes "stop" for exactly that
--     polysemy — seeding it would trade the screen's precision for two fixtures.
--
-- Consumers (all LIVE on this branch): main.py::_turn_matches_grown_correction_cue and
-- main.py::_cessative_shape_admitted (the FOSS-012 decline screens); _tenant_correction_signals
-- and _spacy_cue_route (the pre-existing dual-gate screens — the same rows strengthen
-- the existing correction routing; nothing reads these rows and no-ops).
--
-- Idempotent: ON CONFLICT DO NOTHING on every store; tenant copy inserts ONLY the rows
-- this migration seeds (a tenant-grown or corrected row is never touched).

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.correction_signals
    (pattern, pattern_type, priority, confidence, category, example_usage, notes)
VALUES
  -- Reclarification: "Actually/Wait/Sorry + fact" (mirrors 032's en rows)
  ('en realidad',   'reclarification', 1, 0.85, NULL, 'En realidad trabajo para Acme',   'Spanish "actually" — explicit reclarification marker (NGLE §30.12 reformulation)'),
  ('en cambio',    'reclarification', 2, 0.80, NULL, 'En cambio mi puerto es el 9005', 'Spanish contrastive reformulation ("whereas / on the contrary")'),
  ('espera,' ,     'reclarification', 2, 0.75, NULL, 'Espera, mi nombre es Alejandro', 'Spanish "wait" with comma — mirrors the en seed pair'),
  ('espera',       'reclarification', 2, 0.75, NULL, 'Espera mi nombre es Alejandro',  'Spanish "wait" without comma'),
  ('perdón,'  ,    'reclarification', 2, 0.75, NULL, 'Perdón, no tenemos mascotas',    'Spanish "sorry" with comma (accented)'),
  ('perdón',       'reclarification', 2, 0.75, NULL, 'Perdón no tenemos mascotas',     'Spanish "sorry" (accented)'),
  ('perdon,' ,     'reclarification', 2, 0.75, NULL, 'Perdon, no tenemos mascotas',    'Spanish "sorry" with comma (unaccented typing)'),
  ('perdon',       'reclarification', 2, 0.75, NULL, 'Perdon no tenemos mascotas',     'Spanish "sorry" (unaccented typing)'),
  ('disculpa,' ,   'reclarification', 2, 0.75, NULL, 'Disculpa, no tenemos mascotas',  'Spanish "sorry / excuse me" with comma'),
  ('disculpa',     'reclarification', 2, 0.75, NULL, 'Disculpa no tenemos mascotas',   'Spanish "sorry / excuse me"'),
  ('quería decir', 'reclarification', 1, 0.80, NULL, 'Quería decir Quinn',             'Spanish "I meant" — explicit intention marker'),
  ('queria decir', 'reclarification', 1, 0.80, NULL, 'Queria decir Quinn',             'Spanish "I meant" (unaccented typing)'),
  -- Contradiction: "I was wrong about X"
  ('me equivoqué', 'contradiction',   1, 0.85, NULL, 'Me equivoqué de edad',           'Spanish "I was wrong" — explicit contradiction'),
  ('me equivoque', 'contradiction',   1, 0.85, NULL, 'Me equivoque de edad',           'Spanish "I was wrong" (unaccented typing)'),
  ('mi error',     'contradiction',   2, 0.75, NULL, 'Mi error con la edad',           'Spanish "my mistake" — informal contradiction'),
  -- Blanket negation (mirrors the en "not true"/"not right" rows)
  ('no es verdad', 'negation',        2, 0.80, NULL, 'Eso no es verdad',               'Spanish blanket negation'),
  ('no es cierto', 'negation',        2, 0.80, NULL, 'Eso no es cierto',               'Spanish blanket negation')
ON CONFLICT (pattern) DO NOTHING;

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('dejar',     'cessative_verb', 'xcomp_progressive|direct_object|intransitive', 'dejé de fumar; dejé el grupo', 'seed_cessative_verb_es', 0.90),
  ('abandonar', 'cessative_verb', 'xcomp_progressive|direct_object|intransitive', 'abandoné el grupo',            'seed_cessative_verb_es', 0.85),
  ('cesar',     'cessative_verb', 'xcomp_progressive|direct_object|intransitive', 'cesé de publicar',             'seed_cessative_verb_es', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — 282's pattern
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
                   WHERE table_schema = _schema AND table_name = 'correction_signals') THEN
            EXECUTE format($cs$
                INSERT INTO %I.correction_signals
                    (pattern, pattern_type, applicable_rel_types, priority, confidence,
                     category, example_usage, notes)
                SELECT pattern, pattern_type, applicable_rel_types, priority, confidence,
                       category, example_usage, notes
                FROM public.correction_signals
                WHERE pattern IN ('en realidad', 'en cambio', 'espera,', 'espera',
                                  'perdón,', 'perdón', 'perdon,', 'perdon',
                                  'disculpa,', 'disculpa', 'quería decir',
                                  'queria decir', 'me equivoqué', 'me equivoque',
                                  'mi error', 'no es verdad', 'no es cierto')
                ON CONFLICT (pattern) DO NOTHING
            $cs$, _schema);
        END IF;
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
                WHERE category = 'cessative_verb'
                  AND cue IN ('dejar', 'abandonar', 'cesar')
                ON CONFLICT (cue, category) DO NOTHING
            $seed$, _schema);
        END IF;
        RAISE NOTICE 'Migration 293: Spanish correction-evidence seeds applied in %', _schema;
    END LOOP;
END $$;

