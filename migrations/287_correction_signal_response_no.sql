-- Migration 287: correction_signals — seed the turn-initial response particle "no," as a
-- reclarification cue (lane I, issue #135).
--
-- WHY
-- ---
-- "I scored 1999 points." then "No, I scored 2001 points." — the second turn is the speaker
-- repairing their own earlier statement, but it reached /ingest on the STATEMENT route with no
-- correction flag and the user-scalar overwrite guard refused it ("overwriting it requires
-- stating the turn as a correction"). The turn DOES state itself as one: its clause-initial
-- response particle negates the prior statement, exactly like the seeded reclarification cues
-- 'wait,' / 'sorry,' (migration 032). main.py::_turn_states_repair reads the tenant's own
-- correction_signals rows for the turn opener, so the cue lives in DATA (per tenant, growable
-- and retirable), never in code.
--
-- Idempotent: ON CONFLICT (pattern) DO NOTHING in public and in every existing tenant schema.
-- New tenants inherit it from public at provisioning.

INSERT INTO public.correction_signals
    (pattern, pattern_type, priority, confidence, category, example_usage, notes)
VALUES
    ('no,', 'reclarification', 2, 0.75, NULL, 'No, I scored 2001 points',
     'Turn-initial negative response particle repairing the speaker''s prior statement')
ON CONFLICT (pattern) DO NOTHING;

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        IF to_regclass(format('%I.correction_signals', _schema)) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format($seed$
            INSERT INTO %I.correction_signals
                (pattern, pattern_type, priority, confidence, category, example_usage, notes)
            SELECT pattern, pattern_type, priority, confidence, category, example_usage, notes
            FROM public.correction_signals
            WHERE pattern = 'no,'
            ON CONFLICT (pattern) DO NOTHING
        $seed$, _schema);
    END LOOP;
END $$;
