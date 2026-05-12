INSERT INTO rel_types (rel_type, label, engine_generated, confidence, source, correction_behavior, category, head_types, tail_types)
VALUES
    ('height',      'Height',      false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('weight',      'Weight',      false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('age',         'Age',         false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('has_gender',  'Gender',      false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('born_on',     'Born On',     false, 1.0, 'builtin', 'supersede', 'temporal', ARRAY['Person'], ARRAY['SCALAR']),
    ('nationality', 'Nationality', false, 1.0, 'builtin', 'supersede', 'identity', ARRAY['Person'], ARRAY['SCALAR']),
    ('occupation',  'Occupation',  false, 1.0, 'builtin', 'supersede', 'work',     ARRAY['Person'], ARRAY['SCALAR'])
ON CONFLICT (rel_type) DO UPDATE SET
    category   = EXCLUDED.category,
    head_types = EXCLUDED.head_types,
    tail_types = EXCLUDED.tail_types,
    source     = EXCLUDED.source;
