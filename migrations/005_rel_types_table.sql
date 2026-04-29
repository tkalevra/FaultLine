-- Dynamic relationship types table
CREATE TABLE IF NOT EXISTS rel_types (
    rel_type          TEXT PRIMARY KEY,
    label             TEXT NOT NULL,
    wikidata_pid      TEXT,
    allowed_head      TEXT[],
    allowed_tail      TEXT[],
    engine_generated  BOOLEAN NOT NULL DEFAULT false,
    confidence        FLOAT NOT NULL DEFAULT 1.0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rel_types_engine_generated
    ON rel_types (engine_generated);

-- Add unique constraint to pending_types.rel_type for ON CONFLICT handling
ALTER TABLE pending_types DROP CONSTRAINT IF EXISTS pending_types_rel_type_unique;
ALTER TABLE pending_types ADD CONSTRAINT pending_types_rel_type_unique UNIQUE (rel_type);
CREATE INDEX IF NOT EXISTS idx_pending_types_rel_type ON pending_types (rel_type);

-- Seed with current SEED_ONTOLOGY
INSERT INTO rel_types (rel_type, label, wikidata_pid, engine_generated) VALUES
    ('is_a',           'is a type of',        'P31',   false),
    ('part_of',        'is a part of',        'P361',  false),
    ('created_by',     'was created by',      'P170',  false),
    ('works_for',      'works for',           'P108',  false),
    ('parent_of',      'is the parent of',    'P40',   false),
    ('child_of',       'is the child of',     'P40',   false),
    ('spouse',         'is married to',       'P26',   false),
    ('sibling_of',     'is a sibling of',     'P3373', false),
    ('also_known_as',  'is also known as',    'P742',  false),
    ('related_to',     'is related to',       'P1659', false),
    ('likes',          'likes',               null,    false),
    ('dislikes',       'dislikes',            null,    false),
    ('prefers',        'prefers',             null,    false),
    ('owns',           'owns',                'P1830', false),
    ('located_in',     'is located in',       'P131',  false),
    ('educated_at',    'was educated at',     'P69',   false),
    ('nationality',    'has nationality',     'P27',   false),
    ('occupation',     'has occupation',      'P106',  false),
    ('born_on',        'was born on',         'P569',  false),
    ('age',            'has age',             null,    false),
    ('knows',          'knows',               'P1891', false),
    ('friend_of',      'is friends with',     null,    false),
    ('met',            'has met',             null,    false),
    ('lives_in',       'lives in',            'P551',  false),
    ('born_in',        'was born in',         'P19',   false),
    ('has_gender',     'has gender',          'P21',   false)
ON CONFLICT (rel_type) DO NOTHING;
