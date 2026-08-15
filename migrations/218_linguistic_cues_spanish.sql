-- Migration 218: Spanish (es) linguistic cue + temporal pattern seeds.
--
-- The spine deriver is scheme-portable (UD v2 labels read alongside the ClearNLP/Penn labels the
-- English chains were written against), so the CODE needs no Spanish word lists. What Spanish needs
-- is the same thing English has: its lexical classes grown in the DATABASE (per-tenant, growable),
-- exactly like the English seeds in migrations 103/105/109/117/118/142. These rows seed public and
-- provisioning copies them into each tenant schema at create time (the growth rail).
--
-- Every rel mapping below is grounded in a primary source:
--   • kinship_noun (the person↔person kinship class, description = noun→rel_type map):
--       madre→parent_of / padre→parent_of  (NGLE §12.5 los sustantivos de parentesco; the rel is the
--       same parent_of the English mother/father rows seed — a stored fact is not "in English")
--       hermana/hermano→sibling_of, hija/hijo→child_of, esposa/esposo→spouse, tía/tío, abuela/abuelo,
--       prima/primo, sobrina/sobrino, nieta/nieto → related_to (the walk resolves the specific tie).
--   • unit_scalar (unit noun → scalar rel_type):
--       año→age  (NGLE: "tener + N años" is THE Spanish age construction — the year unit maps to the
--       same age rel the English year→age row seeds), metro→height, kilo→weight, kilómetro→distance,
--       minuto/hora/día/semana/mes → duration (unit_scalar rows grown per-tenant for the measure lane).
--   • naming_verb (pronominal naming): llamar/llamarse — the UD expl:pv page: an inherently reflexive
--       verb always occurs with a reflexive clitic attached as expl:pv; "llamarse" is the canonical
--       Spanish naming verb ("me llamo Marco", "se llama Rex"). Mirrors the English call/name rows.
--   • possession_verb (stative possession, for the tener-measure chain): tener — NGLE §33.4a-b
--       (pro-drop, Person=1 on the verb) + the tener+age construction; the same possession class the
--       English have/own rows seed.
--   • temporal_patterns (formal_absolute month names + relative cues, the DB-growable temporal rail):
--       Spanish month names (enero…diciembre) and the deictic relative cues dateparser's Spanish
--       locale resolves (ayer, mañana, hoy, ahora, hace N, el año pasado…) — DATE_ORDER is already
--       DMY on the es install (the 3/4/2026 vs 4 March 2026 test), dateparser is already language-
--       pinned to es, and the es NER emits no DATE spans, so the DB cues are what open the date lane.
--
-- All ON CONFLICT (cue, category) / (pattern_regex, anchor_type) / (rel_type) / (alias) DO NOTHING
-- (each matches its table's real unique constraint — rel_type_aliases is UNIQUE on alias alone,
-- a fact the round-1 version got wrong) — idempotent, safe to re-run, no interference with
-- tenant-grown rows (the tenant overlay wins at runtime).

-- ── kinship_noun: Spanish person↔person kinship roles ─────────────────────────────
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('madre',      'kinship_noun', 'parent_of',  'mi madre',             'seed_kinship', 0.95),
  ('padre',      'kinship_noun', 'parent_of',  'mi padre',             'seed_kinship', 0.95),
  ('mamá',       'kinship_noun', 'parent_of',  'mi mamá',              'seed_kinship', 0.92),
  ('mama',       'kinship_noun', 'parent_of',  'mi mama',              'seed_kinship', 0.92),
  ('papá',       'kinship_noun', 'parent_of',  'mi papá',              'seed_kinship', 0.92),
  ('papa',       'kinship_noun', 'parent_of',  'mi papa',              'seed_kinship', 0.92),
  ('hermana',    'kinship_noun', 'sibling_of', 'mi hermana',           'seed_kinship', 0.93),
  ('hermano',    'kinship_noun', 'sibling_of', 'mi hermano',           'seed_kinship', 0.93),
  ('hermanas',   'kinship_noun', 'sibling_of', 'mis hermanas',         'seed_kinship', 0.88),
  ('hermanos',   'kinship_noun', 'sibling_of', 'mis hermanos',         'seed_kinship', 0.88),
  ('hija',       'kinship_noun', 'child_of',   'mi hija',              'seed_kinship', 0.90),
  ('hijo',       'kinship_noun', 'child_of',   'mi hijo',              'seed_kinship', 0.90),
  ('hijas',      'kinship_noun', 'child_of',   'mis hijas',            'seed_kinship', 0.88),
  ('hijos',      'kinship_noun', 'child_of',   'mis hijos',            'seed_kinship', 0.88),
  ('esposa',     'kinship_noun', 'spouse',     'mi esposa',            'seed_kinship', 0.93),
  ('esposo',     'kinship_noun', 'spouse',     'mi esposo',            'seed_kinship', 0.93),
  ('mujer',      'kinship_noun', 'spouse',     'mi mujer',             'seed_kinship', 0.85),
  ('marido',     'kinship_noun', 'spouse',     'mi marido',            'seed_kinship', 0.90),
  ('tía',        'kinship_noun', 'related_to', 'mi tía',               'seed_kinship', 0.88),
  ('tia',        'kinship_noun', 'related_to', 'mi tia',               'seed_kinship', 0.88),
  ('tío',        'kinship_noun', 'related_to', 'mi tío',               'seed_kinship', 0.88),
  ('tio',        'kinship_noun', 'related_to', 'mi tio',               'seed_kinship', 0.88),
  ('abuela',     'kinship_noun', 'related_to', 'mi abuela',            'seed_kinship', 0.88),
  ('abuelo',     'kinship_noun', 'related_to', 'mi abuelo',            'seed_kinship', 0.88),
  ('prima',      'kinship_noun', 'related_to', 'mi prima',             'seed_kinship', 0.85),
  ('primo',      'kinship_noun', 'related_to', 'mi primo',             'seed_kinship', 0.85),
  ('sobrina',    'kinship_noun', 'related_to', 'mi sobrina',           'seed_kinship', 0.85),
  ('sobrino',    'kinship_noun', 'related_to', 'mi sobrino',           'seed_kinship', 0.85),
  ('nieta',      'kinship_noun', 'related_to', 'mi nieta',             'seed_kinship', 0.85),
  ('nieto',      'kinship_noun', 'related_to', 'mi nieto',             'seed_kinship', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ── unit_scalar: Spanish measurement units → scalar rel_type ──────────────────────
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('año',        'unit_scalar', 'age',      'tengo 34 años',        'seed_unit_scalar', 0.92),
  ('años',       'unit_scalar', 'age',      'tengo 34 años',        'seed_unit_scalar', 0.92),
  ('ano',        'unit_scalar', 'age',      'tengo 34 anos',        'seed_unit_scalar', 0.92),
  ('anos',       'unit_scalar', 'age',      'tengo 34 anos',        'seed_unit_scalar', 0.92),
  ('metro',      'unit_scalar', 'height',   'mido 1.80 metros',     'seed_unit_scalar', 0.80),
  ('metros',     'unit_scalar', 'height',   'mido 1.80 metros',     'seed_unit_scalar', 0.80),
  ('kilo',       'unit_scalar', 'weight',   'peso 80 kilos',        'seed_unit_scalar', 0.80),
  ('kilos',      'unit_scalar', 'weight',   'peso 80 kilos',        'seed_unit_scalar', 0.80),
  ('kilogramo',  'unit_scalar', 'weight',   'peso 80 kilogramos',   'seed_unit_scalar', 0.80),
  ('kilogramos', 'unit_scalar', 'weight',   'peso 80 kilogramos',   'seed_unit_scalar', 0.80),
  ('kilómetro',  'unit_scalar', 'distance', 'corrí 5 kilómetros',   'seed_unit_scalar', 0.80),
  ('kilómetros', 'unit_scalar', 'distance', 'corrí 5 kilómetros',   'seed_unit_scalar', 0.80),
  ('kilometro',  'unit_scalar', 'distance', 'corri 5 kilometros',   'seed_unit_scalar', 0.80),
  ('kilometros', 'unit_scalar', 'distance', 'corri 5 kilometros',   'seed_unit_scalar', 0.80),
  ('minuto',     'unit_scalar', 'duration', 'tarda 20 minutos',     'seed_unit_scalar', 0.80),
  ('minutos',    'unit_scalar', 'duration', 'tarda 20 minutos',     'seed_unit_scalar', 0.80),
  ('hora',       'unit_scalar', 'duration', 'tarda una hora',       'seed_unit_scalar', 0.80),
  ('horas',      'unit_scalar', 'duration', 'tarda dos horas',      'seed_unit_scalar', 0.80),
  ('día',        'unit_scalar', 'duration', 'tarda un día',         'seed_unit_scalar', 0.80),
  ('dias',       'unit_scalar', 'duration', 'tarda dos dias',       'seed_unit_scalar', 0.80),
  ('semana',     'unit_scalar', 'duration', 'tarda una semana',     'seed_unit_scalar', 0.80),
  ('semanas',    'unit_scalar', 'duration', 'llevo tres semanas',   'seed_unit_scalar', 0.80),
  ('mes',        'unit_scalar', 'duration', 'tarda un mes',         'seed_unit_scalar', 0.80),
  ('meses',      'unit_scalar', 'duration', 'tarda dos meses',      'seed_unit_scalar', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

-- ── naming_verb: the pronominal naming verb (UD expl:pv — inherently reflexive) ─────
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('llamar',    'naming_verb', 'Pronominal naming verb: "se llama X" / "me llamo X"', 'Mi perro se llama Rex', 'seed_naming', 0.95),
  ('llamarse',  'naming_verb', 'Pronominal naming verb (infinitive)',                  'llamarse X',            'seed_naming', 0.90),
  ('apodar',    'naming_verb', 'Dubbing verb: "le apodan X"',                          'le apodan El Loco',     'seed_naming', 0.85),
  ('apodarse',  'naming_verb', 'Dubbing verb (infinitive)',                            'apodarse X',            'seed_naming', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ── possession_verb: the stative possession class (tener — NGLE §33.4a-b pro-drop) ─
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('tener',   'possession_verb', 'Stative possession verb (pro-drop first person: "tengo X")', 'Tengo un perro', 'seed_possession', 0.90),
  ('tengo',   'possession_verb', 'Stative possession verb, 1sg form',                           'Tengo un perro', 'seed_possession', 0.90)
ON CONFLICT (cue, category) DO NOTHING;

-- ── temporal_patterns: Spanish month names (formal_absolute) + deictic relative cues ─
-- The es NER emits no DATE spans (measured), so the date lane depends on these DB cues to open,
-- and dateparser (already pinned es) resolves them. DATE_ORDER=DMY is already the es-install default.
INSERT INTO public.temporal_patterns
    (pattern_regex, anchor_type, description, example_text, category, source, global_confidence)
VALUES
  ('\benero\b',       'absolute_no_year', 'Month: enero (January)',       'el 15 de enero',      'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bfebrero\b',     'absolute_no_year', 'Month: febrero (February)',    'el 3 de febrero',     'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bmarzo\b',       'absolute_no_year', 'Month: marzo (March)',         'el 15 de marzo',      'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bábril\b',       'absolute_no_year', 'Month: abril (April)',         'el 3 de abril',       'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\babril\b',       'absolute_no_year', 'Month: abril (April, no accent)','el 3 de abril',     'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bmayo\b',        'absolute_no_year', 'Month: mayo (May)',            'el 3 de mayo',        'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bjunio\b',       'absolute_no_year', 'Month: junio (June)',          'el 3 de junio',       'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bjulio\b',       'absolute_no_year', 'Month: julio (July)',          'el 3 de julio',       'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bagosto\b',      'absolute_no_year', 'Month: agosto (August)',       'el 3 de agosto',      'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bseptiembre\b',  'absolute_no_year', 'Month: septiembre (September)','el 3 de septiembre',  'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bsetiembre\b',   'absolute_no_year', 'Month: setiembre (variant)',   'el 3 de setiembre',   'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\boctubre\b',     'absolute_no_year', 'Month: octubre (October)',     'el 3 de octubre',     'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bnoviembre\b',   'absolute_no_year', 'Month: noviembre (November)',  'el 3 de noviembre',   'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bdiciembre\b',   'absolute_no_year', 'Month: diciembre (December)',  'el 3 de diciembre',   'formal_absolute', 'seed_dateparser_es', 0.97),
  ('\bayer\b',        'relative', 'Deictic: yesterday',                   'ayer',                'relative_cue',    'seed_dateparser_es', 0.97),
  ('\bmañana\b',      'relative', 'Deictic: tomorrow',                    'mañana',              'relative_cue',    'seed_dateparser_es', 0.97),
  ('\bmanana\b',      'relative', 'Deictic: tomorrow (no accent)',        'manana',              'relative_cue',    'seed_dateparser_es', 0.97),
  ('\bhoy\b',         'relative', 'Deictic: today',                       'hoy',                 'relative_cue',    'seed_dateparser_es', 0.97),
  ('\bahora\b',       'relative', 'Deictic: now',                         'ahora',               'relative_cue',    'seed_dateparser_es', 0.85),
  ('\bhace\b',        'relative', 'Relative: "hace N <units>" (N units ago)', 'hace dos semanas', 'relative_cue', 'seed_dateparser_es', 0.92),
  ('\bel\s+año\s+pasado\b', 'relative', 'Relative: last year',          'el año pasado',       'relative_cue',    'seed_dateparser_es', 0.92),
  ('\bpróxima\s+semana\b',   'relative', 'Relative: next week',          'la próxima semana',   'relative_cue',    'seed_dateparser_es', 0.90),
  ('\bsemana\s+que\s+viene\b', 'relative', 'Relative: the coming week', 'la semana que viene', 'relative_cue',    'seed_dateparser_es', 0.88)
ON CONFLICT (pattern_regex, anchor_type) DO NOTHING;

-- ── rel_types.natural_language: Spanish recall templates (per-tenant growable) ─────────
-- The QUERY walk matches query words against rel_types.natural_language (determine_path,
-- scalar-aspect + relational-aspect grounding). The seeded templates are English ("X is Y
-- years old" -> age), so a Spanish query ("¿cuántos años tengo?") would match nothing and
-- the walk would fetch-all instead of narrowing to the age scalar. These es templates make
-- the walk language-appropriate: the template's content words (años, madre, hermana, ...)
-- are what the query matches, and the render path reads the same column for prose. The
-- natural_language column is per-tenant (seeded from public at provisioning, grown
-- per-tenant) — the same growth rail as the cue classes.
--
-- ⚠️ THIS BRANCH IS THE LANGUAGE (per the es-branch design: the branch IS Spanish). The
-- natural_language column is SINGULAR per rel_type, so these UPDATEs REPLACE the English
-- templates with Spanish ones — NOT add alongside. English recall on this branch is not the
-- target (English installs use master/main, where the templates stay English). This is the
-- same replacement the it branch made for Italian. A query like "how old am I" will not
-- scope age here; "¿cuántos años tengo?" will. That is the branch contract, stated plainly.
UPDATE public.rel_types SET natural_language = 'X tiene Y años' WHERE rel_type = 'age';
UPDATE public.rel_types SET natural_language = 'X es el padre de Y' WHERE rel_type = 'parent_of';
UPDATE public.rel_types SET natural_language = 'X y Y son hermanos' WHERE rel_type = 'sibling_of';
UPDATE public.rel_types SET natural_language = 'X posee Y' WHERE rel_type = 'owns';
UPDATE public.rel_types SET natural_language = 'X siente Y' WHERE rel_type = 'feels';
UPDATE public.rel_types SET natural_language = 'X es una instancia de Y' WHERE rel_type = 'instance_of';
UPDATE public.rel_types SET natural_language = 'X está en estado Y' WHERE rel_type = 'has_state';
UPDATE public.rel_types SET natural_language = 'X también es conocido como Y' WHERE rel_type = 'also_known_as';
UPDATE public.rel_types SET natural_language = 'X mide Y' WHERE rel_type = 'height';
UPDATE public.rel_types SET natural_language = 'X pesa Y' WHERE rel_type = 'weight';
UPDATE public.rel_types SET natural_language = 'X vive en Y' WHERE rel_type = 'lives_in';
UPDATE public.rel_types SET natural_language = 'X se encuentra en Y' WHERE rel_type = 'located_in';
-- Spanish WEEKDAY names — a closed calendar class (like the month names above): seeded as
-- temporal_patterns rows so the date lane opens for weekday-anchored dates ("el lunes tengo
-- una reunión") AND the classification chain can reject a weekday complement as a calendar
-- expression, never a type. Same growable per-tenant rail as the months/relative cues.
INSERT INTO public.temporal_patterns
    (pattern_regex, anchor_type, description, example_text, category, source, global_confidence)
VALUES
  ('\blunes\b',      'relative', 'Weekday: lunes (Monday)',     'el lunes',      'relative_cue', 'seed_dateparser_es', 0.95),
  ('\bmartes\b',     'relative', 'Weekday: martes (Tuesday)',   'el martes',     'relative_cue', 'seed_dateparser_es', 0.95),
  ('\bmiércoles\b',  'relative', 'Weekday: miércoles (Wednesday)', 'el miércoles','relative_cue', 'seed_dateparser_es', 0.95),
  ('\bmiercoles\b',  'relative', 'Weekday: miércoles (no accent)','el miercoles','relative_cue', 'seed_dateparser_es', 0.95),
  ('\bjueves\b',     'relative', 'Weekday: jueves (Thursday)',  'el jueves',     'relative_cue', 'seed_dateparser_es', 0.95),
  ('\bviernes\b',    'relative', 'Weekday: viernes (Friday)',   'el viernes',    'relative_cue', 'seed_dateparser_es', 0.95),
  ('\bsábado\b',     'relative', 'Weekday: sábado (Saturday)',  'el sábado',     'relative_cue', 'seed_dateparser_es', 0.95),
  ('\bsabado\b',     'relative', 'Weekday: sábado (no accent)', 'el sabado',     'relative_cue', 'seed_dateparser_es', 0.95),
  ('\bdomingo\b',    'relative', 'Weekday: domingo (Sunday)',   'el domingo',    'relative_cue', 'seed_dateparser_es', 0.95)
ON CONFLICT (pattern_regex, anchor_type) DO NOTHING;
-- Spanish residence predicate ALIAS: the folded SVO predicate "vivir_en" (verb lemma +
-- load-bearing particle "en") maps to the CANONICAL lives_in rel, exactly like the English
-- "live_in" folds to lives_in via the same alias rail (English seeds live/lives -> lives_in;
-- there is deliberately NO "live_in" rel_type — an alias-only fold). The SVO lane emits
-- (user, vivir_en, madrid); /ingest's canonical backstop (resolve_canonical RUNG 3) folds
-- the alias to lives_in before storage, so the walk's rel-type projection ("¿dónde vives?" /
-- "where do you live" -> lives_in) admits the edge.
-- ⚠️ NO rel_type row for "vivir_en": an exact rel_types PK would SHADOW the alias at RUNG 2
-- (exact wins before alias), the edge would store as vivir_en, and the walk (which projects
-- lives_in) would never return it — measured. Alias-only, mirroring English live_in.
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence)
VALUES ('lives_in', 'vivir_en', 'es_seed', 0.95)
ON CONFLICT (alias) DO NOTHING;
-- Spanish residence VERB-FORM aliases: the query walk resolves a query word to a rel via
-- rel_type_aliases (determine_path's keyword→rel lane), the SAME mechanism the English
-- seeds use ("live"/"lives" → lives_in). Spanish needs its own closed inflectional forms of
-- "vivir" so "¿dónde vivo?" / "¿dónde vives?" / "¿dónde viven?" scope lives_in exactly like
-- "where do I live?". Closed verb-paradigm forms of ONE lemma, seeded per-tenant on the same
-- alias rail (growable) — NOT a code word list.
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence)
VALUES
  ('lives_in', 'vivo',   'es_seed', 0.95),
  ('lives_in', 'vives',  'es_seed', 0.95),
  ('lives_in', 'vive',   'es_seed', 0.95),
  ('lives_in', 'vivimos','es_seed', 0.95),
  ('lives_in', 'viven',  'es_seed', 0.95)
ON CONFLICT (alias) DO NOTHING;
-- Spanish employment predicate ALIAS: the folded SVO predicate "trabajar_en" / "trabajar_para"
-- (verb lemma + load-bearing particle "en"/"para") maps to the CANONICAL works_for rel, exactly
-- like the English "work_for" folds via the /ingest seeded-morphology fold. Alias-only (NO
-- rel_type row — an exact PK would shadow the alias at RUNG 2, the vivre_en lesson). Without
-- this, "Yo trabajo en Google" emits (user, trabajar_en, google) under a novel unseeded rel
-- that the walk's works_for projection never admits — the fold-arm comment's "exactly like
-- English" claim was false until these rows existed (critic round-2 blocker).
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence)
VALUES
  ('works_for', 'trabajar_en',   'es_seed', 0.95),
  ('works_for', 'trabajar_para', 'es_seed', 0.95)
ON CONFLICT (alias) DO NOTHING;
-- Spanish employment VERB-FORM aliases (query walk): "¿dónde trabajas?" / "¿dónde trabaja?"
-- scope works_for exactly like "where do you work?" (the English live/lives->lives_in mechanism).
-- Closed inflectional forms of ONE lemma (trabajar), seeded per-tenant, NOT a code word list.
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence)
VALUES
  ('works_for', 'trabajo',   'es_seed', 0.95),
  ('works_for', 'trabajas',  'es_seed', 0.95),
  ('works_for', 'trabaja',   'es_seed', 0.95),
  ('works_for', 'trabajamos','es_seed', 0.95),
  ('works_for', 'trabajan',  'es_seed', 0.95)
ON CONFLICT (alias) DO NOTHING;
-- Spanish load-bearing particles (svo_particle cue class): the es model attaches the
-- preposition as a case child of the oblique nominal ("vive EN Madrid"), and the SVO
-- predicate-folding + object-read seams only fold/read particles in this DB class. English
-- seeds "in/at/on/..."; Spanish needs its own ("en", "a", "de", "con", "para", "por") so
-- "vivir en" folds like English "live in" and the residence edge surfaces. Same growable
-- per-tenant rail as every other cue class.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('en',     'svo_particle', 'Load-bearing particle: "vivir en <ciudad>"', 'Vivo en Madrid', 'seed_svo_particle_es', 0.90),
  ('a',      'svo_particle', 'Load-bearing particle: "ir a <lugar>"',       'Voy a Madrid',   'seed_svo_particle_es', 0.90),
  ('de',     'svo_particle', 'Load-bearing particle: "venir de <lugar>"',   'Vengo de Madrid','seed_svo_particle_es', 0.90),
  ('con',    'svo_particle', 'Load-bearing particle: "quedar con <alguien>"','Quedo con Ana','seed_svo_particle_es', 0.90),
  ('para',   'svo_particle', 'Load-bearing particle: "trabajar para <org>"','Trabajo para IBM','seed_svo_particle_es', 0.90),
  ('por',    'svo_particle', 'Load-bearing particle: "pasar por <lugar>"',  'Paso por casa',  'seed_svo_particle_es', 0.90)
ON CONFLICT (cue, category) DO NOTHING;
