-- Pre-seed medical domain rel_types (dprompt-76b / dBug-015)
-- Medical rel_types were extracted by LLM but rejected as novel candidates
-- because they were not pre-seeded in rel_types table. frequency < 3 and
-- similarity < 0.85 → rejected by re_embedder. Pre-seeding gives them
-- immediate availability at startup.

-- Idempotent: ON CONFLICT (rel_type) DO NOTHING — safe to re-run

INSERT INTO rel_types (rel_type, label, wikidata_pid, confidence,
                        storage_target, fact_class,
                        is_symmetric, is_leaf_only, is_hierarchy_rel,
                        head_types, tail_types,
                        correction_behavior, source)
VALUES
    ('has_medical_condition', 'Medical Condition', 'P1050', 0.8,
     'facts', 'B',
     false, false, false,
     ARRAY['Person'], ARRAY['Concept'],
     'supersede', 'builtin'),
    ('has_symptom', 'Symptom', 'P780', 0.8,
     'facts', 'B',
     false, false, false,
     ARRAY['Person'], ARRAY['Concept'],
     'supersede', 'builtin'),
    ('has_injury', 'Injury', NULL, 0.8,
     'facts', 'B',
     false, false, false,
     ARRAY['Person'], ARRAY['Concept'],
     'supersede', 'builtin'),
    ('affected_body_part', 'Affected Body Part', 'P927', 0.8,
     'facts', 'B',
     false, false, false,
     ARRAY['Person'], ARRAY['Concept'],
     'supersede', 'builtin'),
    ('has_medication', 'Medication', 'P5002', 0.8,
     'facts', 'B',
     false, false, false,
     ARRAY['Person'], ARRAY['Concept'],
     'supersede', 'builtin'),
    ('has_allergy', 'Allergy', NULL, 0.8,
     'facts', 'B',
     false, false, false,
     ARRAY['Person'], ARRAY['Concept'],
     'supersede', 'builtin')
ON CONFLICT (rel_type) DO NOTHING;
