-- Migration 265: brain_probe_state.fault — WHAT ACTUALLY HAPPENED, kept alongside THAT it failed
-- Date: 2026-08-18
--
-- WHY
-- ---
-- Migration 204 gave every seat an alarm stream that answers "is my brain broken right now".
-- It answers that well. It structurally CANNOT answer "broken HOW", because the row records an
-- OUTCOME (ok/fail), a latency and a budget, and nothing about the failure itself. So the
-- tenant-facing notice built on it had exactly one story available for every failure — the
-- story it had words for — and it told that story regardless of what happened.
--
-- Measured on production, 2026-08-17. A tenant's endpoint answered:
--
--     HTTP 402 Payment Required, in 90ms, body:
--     "Payment required to access this resource. Visit your billing tab."
--
-- and the tenant was told the call "timed out after 0.081s", that "the budget for this step is
-- 8s", and to "check that api.cerebras.ai is reachable from FaultLine — allow it through the
-- firewall if it is behind one". Nothing timed out. The budget was never approached. The host
-- was reachable. The account was unpaid, and the provider had said so, in its own words, in a
-- body that reached the log line and stopped there. A customer following that advice spends an
-- hour on network configuration while the actual fault goes unmentioned.
--
-- The detection existed. The surfacing did not. Same shape as the credential-undecryptable
-- critical that fired into a container log for two weeks — except this one did not stay silent,
-- it spoke and was wrong, which is worse.
--
-- WHAT THIS COLUMN IS
-- -------------------
-- `fault` is the DERIVED description of the most recent failure on that lane, as produced by
-- `saas/brain_fault.py::describe` from the exception and the response that caused it. JSONB,
-- because the set of derivable facts is open: a provider that starts sending `Retry-After`
-- should not need a migration to be believed.
--
--     {"kind": "refused",
--      "error_type": "HTTPStatusError",
--      "status": 402,
--      "status_phrase": "Payment Required",     -- http.HTTPStatus, i.e. the IANA registry
--      "status_class": 4,                       -- RFC 9110 §15.5 vs §15.6
--      "provider_message": "Payment required to access this resource. Visit your billing tab."}
--
--     {"kind": "unreachable", "error_type": "ConnectError",
--      "transport_detail": "[Errno 111] Connection refused"}
--
--     {"kind": "timeout",  "error_type": "ReadTimeout"}
--     {"kind": "unknown",  "error_type": "RuntimeError"}   -- we do not know. It says so.
--
-- `kind` is one of refused | timeout | unreachable | connection_lost | unknown, and it is
-- derived — from whether a response object exists at all, and from httpx's own exception
-- taxonomy. It is NOT a status-code lookup, and a status-code lookup must never be added: that
-- is the next version of this bug, wrong for every provider that uses a code loosely and silent
-- on the code nobody has added yet.
--
-- NULL IS A REAL VALUE AND IT MEANS "we did not record why". Every row written before this
-- migration reads NULL, and so does a failure whose cause could not be derived. The renderer
-- treats NULL as "not known" and says so, rather than falling back to the old sentence — a
-- fallback that fills the gap with a guess is the defect, not the fix.
--
-- PRIVACY / SECURITY
-- ------------------
-- `provider_message` is the PROVIDER's text, not the tenant's, and a provider is entitled to
-- echo the credential it just rejected back at us (OpenAI answers a bad key with "Incorrect API
-- key provided: sk-…"). `brain_fault._redact` destroys credential-shaped runs BEFORE the value
-- is stored, so nothing secret reaches this column, the console card, or the model-visible
-- notice. `transport_detail` is passed through the same filter. Neither ever carries a URL
-- path or query string — the host lives in `endpoint_host`, which migration 204 already
-- restricts to HOST[:PORT] for exactly this reason.
--
-- PER-TENANT, like the table it extends: the request search_path has NO public, so the column
-- must exist inside every tenant schema. New tenants get it from
-- src/provisioning/templates/user_schema.sql, updated in this same change (migration 197
-- shipped migration-only and silently skipped every NEW tenant; that is not repeated here).
--
-- GUARDED FANOUT: driven off information_schema.schemata, so a stale registry row naming a
-- schema that no longer exists cannot abort the whole DO block. Additive and idempotent
-- (ADD COLUMN IF NOT EXISTS); no DROP, no UPDATE, no destructive statement. Safe to re-run.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip a tenant that predates migration 204 (no alarm stream yet). It will get the
        -- column with the table when 204's own fanout reaches it.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'brain_probe_state'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.brain_probe_state ADD COLUMN IF NOT EXISTS fault JSONB',
            _schema);

        EXECUTE format(
            'COMMENT ON COLUMN %I.brain_probe_state.fault IS %L',
            _schema,
            'Derived description of the most recent FAILURE on this lane '
            '(saas/brain_fault.py::describe): kind (refused|timeout|unreachable|'
            'connection_lost|unknown), the HTTP status and its RFC reason phrase when the '
            'endpoint answered, the provider''s OWN message out of the response body, and the '
            'transport''s own words when it never answered. NULL = the cause was not recorded '
            'or could not be derived, and the tenant-facing notice says exactly that rather '
            'than guessing. NEVER a status-code-to-copy table.');
    END LOOP;
END $$;
