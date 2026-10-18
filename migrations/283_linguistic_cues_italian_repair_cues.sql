-- Migration 283 (it branch): Italian repair-marker / retraction-verb cue screens for the
-- FOSS-011 first-mention naming protection (critic round 1).
--
-- The FOSS-011 fix routes a FIRST-MENTION naming declarative to STATEMENT before GLiNER2. The
-- critic's live repro: the preroute's only split-turn protection was the bootstrap cue seed,
-- which is ENGLISH-ONLY ('forget'/'delete'/'actually'/'i meant' - zero Italian), so the true
-- split-turn correction b2 "Anzi, si chiama DevBox." and the plain-language forget
-- "dimentica che ho una gatta che si chiama Luna" were both pre-routed STATEMENT and the
-- turn never reached the correction/retraction ladder.
--
-- Rule (same rail as migration 282 / every cue migration): the CODE carries no Italian word
-- list; the members of each closed lexical class live in the DATABASE (public template ->
-- copied into every tenant schema at provisioning, back-filled for EXISTING tenants by Part 2
-- below, grown/retired per tenant via /structure/deactivate-cue). Both classes are registered
-- in _BOOTSTRAP_BY_CATEGORY with DELIBERATELY EMPTY floors (linguistic_cue_overlay.py): a cold
-- or DB-down read resolves EMPTY, never the naming-verb fallback.
--
-- Consumers (src/api/main.py): the NAMING_STATEMENT_PREROUTE decider
-- (_is_first_mention_naming_statement) and the /retract/correct first-mention guard
-- (_first_mention_naming_correction) both decline (defer to the full ladder) when the turn
-- carries any of these cues, matched as lowercased SUBSTRINGS (phrase semantics - 'a dire il
-- vero' is four tokens; substring matching is the legacy correction_patterns discipline). A
-- match is ALWAYS the conservative direction: the turn falls through to GLiNER2 + the
-- correction/retraction ladder; it never routes destructive by itself. Polysemy cost is
-- therefore bounded (an over-deferred turn classifies exactly as it did before FOSS-011).
--
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING.

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('anzi',            'repair_marker', 'Repair marker: the turn replaces what was just said.',        'Il mio server si chiama Apollo. Anzi, si chiama DevBox.', 'seed_repair_marker_it', 0.95),
  ('anzi no',         'repair_marker', 'Emphatic negating repair marker.',                            'Anzi no, ho sbagliato io.',                                'seed_repair_marker_it', 0.90),
  ('anzi meglio',     'repair_marker', 'Repair marker proposing the better version.',                 'Anzi meglio, chiamalo DevBox.',                            'seed_repair_marker_it', 0.90),
  ('in realtà',       'repair_marker', 'Repair marker: in fact / actually (accented).',               'In realtà si chiama DevBox.',                              'seed_repair_marker_it', 0.92),
  ('in realta',       'repair_marker', 'Repair marker: unaccented variant of in realtà.',             'in realta si chiama DevBox',                               'seed_repair_marker_it', 0.90),
  ('a dire il vero',  'repair_marker', 'Repair marker: to tell the truth.',                           'A dire il vero il server si chiama DevBox.',               'seed_repair_marker_it', 0.90),
  ('semmai',          'repair_marker', 'Repair marker: if anything.',                                 'Semmai è il portatile, non il server.',                    'seed_repair_marker_it', 0.85),
  ('volevo dire',     'repair_marker', 'Repair marker: I meant to say (the it counterpart of en "i meant").', 'Volevo dire DevBox.',                              'seed_repair_marker_it', 0.92),
  ('ho sbagliato',    'repair_marker', 'Repair marker: I was wrong.',                                 'Ho sbagliato, si chiama DevBox.',                          'seed_repair_marker_it', 0.90),
  ('mi sono sbagliato',  'repair_marker', 'Repair marker: I was wrong (masc. speaker).',              'Mi sono sbagliato, è DevBox.',                             'seed_repair_marker_it', 0.90),
  ('mi sono sbagliata',  'repair_marker', 'Repair marker: I was wrong (fem. speaker).',               'Mi sono sbagliata, è DevBox.',                             'seed_repair_marker_it', 0.90),
  ('correzione:',     'repair_marker', 'Explicit correction label (colon form).',                     'Correzione: si chiama DevBox.',                            'seed_repair_marker_it', 0.88),
  ('non è vero',      'repair_marker', 'Truth-negation repair: what was said is not true.',           'Non è vero che si chiama Apollo.',                         'seed_repair_marker_it', 0.88),
  ('non e vero',      'repair_marker', 'Truth-negation repair (unaccented variant).',                 'non e vero che ho un cane',                                'seed_repair_marker_it', 0.85),
  ('dimentica',       'retraction_verb', 'Retraction directive: forget (2sg; substring covers dimenticalo, dimenticarsi, dimenticato).', 'Dimentica che ho una gatta che si chiama Luna.', 'seed_retraction_verb_it', 0.95),
  ('dimentichi',      'retraction_verb', 'Retraction directive: forget (formal/polite).',             'Dimentichi il server Apollo.',                             'seed_retraction_verb_it', 0.92),
  ('dimentichiamo',   'retraction_verb', 'Retraction directive: let us forget.',                      'Dimentichiamoci Apollo.',                                  'seed_retraction_verb_it', 0.85),
  ('cancella',        'retraction_verb', 'Retraction directive: delete (substring covers cancellare).', 'Cancella il server Apollo.',                             'seed_retraction_verb_it', 0.92),
  ('cancelli',        'retraction_verb', 'Retraction directive: delete (formal; prefix of cancelliamo).', 'Cancelli tutto riguardo Apollo.',                       'seed_retraction_verb_it', 0.88),
  ('rimuovi',         'retraction_verb', 'Retraction directive: remove (2sg).',                       'Rimuovi Apollo.',                                          'seed_retraction_verb_it', 0.90),
  ('rimuovere',       'retraction_verb', 'Retraction verb: to remove (infinitive).',                  'Devi rimuovere Apollo.',                                   'seed_retraction_verb_it', 0.85),
  ('rimuova',         'retraction_verb', 'Retraction directive: remove (formal).',                    'Rimuova Apollo dal server.',                               'seed_retraction_verb_it', 0.88),
  ('elimina',         'retraction_verb', 'Retraction directive: eliminate (substring covers eliminare).', 'Elimina la gatta Luna.',                             'seed_retraction_verb_it', 0.88),
  ('elimini',         'retraction_verb', 'Retraction directive: eliminate (formal).',                 'Elimini Apollo.',                                          'seed_retraction_verb_it', 0.85),
  ('togli',           'retraction_verb', 'Retraction directive: take away / remove.',                 'Togli Apollo.',                                            'seed_retraction_verb_it', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas; back-fills the WARM
-- tenants that provisioned before this migration - same rail as 282 Part 2).
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
                WHERE source IN ('seed_repair_marker_it', 'seed_retraction_verb_it')
                ON CONFLICT (cue, category) DO NOTHING
            $seed$, _schema);
        END IF;
        RAISE NOTICE 'Migration 283: FOSS-011 it repair/retraction cue screens applied in %', _schema;
    END LOOP;
END $$;
