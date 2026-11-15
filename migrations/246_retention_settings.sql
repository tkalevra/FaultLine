-- 246_retention_settings.sql — the CONTRACTUAL retention windows as operator-owned settings.
-- CLOSED-SOURCE SaaS layer. Control-plane public.* ONLY. Idempotent; safe to re-run.
--
-- WHY THIS EXISTS
-- ───────────────
-- The signed Terms of Service (migration 243) makes SPECIFIC, LEGALLY-BINDING retention promises:
--
--   §7.1  "Your data is retained, and available for export on request under section 4, for 30 days."
--   §7.2  "At the end of those 30 days it is permanently deleted from our live systems."
--   §7.3  "Any backup copies containing your data are overwritten within a further 35 days."
--   §4    deletion on request / export "within five business days".
--
-- Those numbers were HARDCODED PROSE. The only real retention knob in the platform was
-- ``platform_settings.retention_grace_days`` (DEFAULT 7, migration 179) — which drives
-- saas/retention.py's reaper and applies ONLY to lapsed self-serve INDIVIDUAL trials, never to
-- tenants. So the contract and the configuration disagreed, and the configuration was not
-- reachable from the operator console at all.
--
-- This migration makes the three CONTRACT numbers first-class operator settings, read at publish
-- time by saas/contracts.py::publish_terms_version so a newly published ToS version and the live
-- configuration can never drift apart.
--
-- ⚠️ FOUR DISTINCT WINDOWS — DO NOT CONFLATE THEM (the console must not either):
--
--   retention_grace_days   (mig 179, DEFAULT 7)  INDIVIDUAL TRIAL GRACE. Days past trial_ends_at
--                                                before an UNSUBSCRIBED self-serve individual
--                                                becomes a candidate for the purge reaper. This is
--                                                the ONE value the platform acts on automatically
--                                                — and even that is DRY-RUN unless the operator
--                                                sets SAAS_RETENTION_PURGE_ENABLED.
--   data_retention_days    (here,   DEFAULT 30)  POST-TERMINATION LIVE-DATA RETENTION (ToS §7.1/2).
--                                                A CONTRACTUAL commitment. NOTHING in this codebase
--                                                enforces it today: saas/retention.py reaps
--                                                public.individual_users only, never a tenant.
--   backup_overwrite_days  (here,   DEFAULT 35)  BACKUP OVERWRITE WINDOW (ToS §7.3), counted from
--                                                the end of data_retention_days. A property of the
--                                                BACKUP SYSTEM, which lives outside this codebase
--                                                entirely. Purely a commitment.
--   fulfilment_days        (here,   DEFAULT 5)   EXPORT / DELETION-ON-REQUEST fulfilment window in
--                                                BUSINESS days (ToS §4, §8). Performed by hand by
--                                                the operator. Purely a commitment.
--
-- ⚠️ THE FAIL-SAFE DIRECTION IS *LONGER*, NEVER ZERO. A retention window that resolves to 0 means
-- "delete immediately" — the single worst outcome an unreadable config could produce. Every column
-- below therefore has a CHECK with a floor of 1, and the Python getters
-- (saas/platform_settings.py) resolve an unreadable / out-of-range-low value to the DOCUMENTED
-- DEFAULT (30 / 35 / 5), not to the minimum. See the getters for the full reasoning.

-- ── ALSO HERE: the TENANT contract term ──────────────────────────────────────────────
-- Same class of value, same panel, same publish-time binding: a number the signed agreement
-- states, which had no operator control. The commercial model is a 6-month TENANT term billed
-- monthly by card ("removals are billed to term-end, no refund"), while INDIVIDUALS are
-- month-to-month with a free first month and NO lock-in.
--
-- ⚠️ TENANT TIERS ONLY. An individual is month-to-month BY DESIGN — someone kicking the tires must
-- never be trapped — so this setting must not reach the individual path. Conflating the two is
-- precisely the mistake this column is documented to prevent: a previous change forced self-serve
-- tenants to a 1-month term on the belief that the 6-month default contradicted the contract. It
-- was the CONTRACT TEXT that was wrong, not the term.
--
-- Fail-safe direction is the DEFAULT, never 0: a 0-month term is meaningless and would make every
-- seat instantly cancellable, i.e. it would silently delete the commitment.
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS contract_term_months INTEGER NOT NULL DEFAULT 6;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'platform_settings_contract_term_months_range') THEN
        ALTER TABLE public.platform_settings
            ADD CONSTRAINT platform_settings_contract_term_months_range
            CHECK (contract_term_months >= 1 AND contract_term_months <= 60);
    END IF;
END $$;

COMMENT ON COLUMN public.platform_settings.contract_term_months IS
    'CONTRACTUAL minimum TENANT commitment in months (control-plane public.*). DEFAULT 6, billed '
    'monthly by card; seat removals are billed to term-end. Written into the ToS text at publish '
    'time via the {{CONTRACT_TERM_MONTHS}} placeholder. TENANT TIERS ONLY — individuals are '
    'month-to-month by design and this value must never reach that path. Floor is 1, never 0: a '
    '0-month term is no commitment at all.';

-- ── the three contractual windows ────────────────────────────────────────────────────
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS data_retention_days INTEGER NOT NULL DEFAULT 30;

ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS backup_overwrite_days INTEGER NOT NULL DEFAULT 35;

ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS fulfilment_days INTEGER NOT NULL DEFAULT 5;

-- ── range CHECKs — the DB refuses a nonsense window even if a caller bypasses the API ──
-- Floors are 1, never 0, for the reason in the header: 0 days of retention is "delete now".
-- Added via DO blocks so the migration stays re-runnable (ADD CONSTRAINT has no IF NOT EXISTS).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'platform_settings_data_retention_days_range') THEN
        ALTER TABLE public.platform_settings
            ADD CONSTRAINT platform_settings_data_retention_days_range
            CHECK (data_retention_days >= 1 AND data_retention_days <= 3650);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'platform_settings_backup_overwrite_days_range') THEN
        ALTER TABLE public.platform_settings
            ADD CONSTRAINT platform_settings_backup_overwrite_days_range
            CHECK (backup_overwrite_days >= 1 AND backup_overwrite_days <= 3650);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'platform_settings_fulfilment_days_range') THEN
        ALTER TABLE public.platform_settings
            ADD CONSTRAINT platform_settings_fulfilment_days_range
            CHECK (fulfilment_days >= 1 AND fulfilment_days <= 30);
    END IF;
END $$;

COMMENT ON COLUMN public.platform_settings.data_retention_days IS
    'CONTRACTUAL post-termination LIVE-DATA retention window in days (ToS section 7.1/7.2; '
    'control-plane public.*). DEFAULT 30. Written into the ToS text at publish time by '
    'saas/contracts.py::publish_terms_version via the {{RETENTION_DAYS}} placeholder. NOT ENFORCED '
    'AUTOMATICALLY — saas/retention.py reaps lapsed individual trials only, never a tenant. '
    'Floor is 1, never 0: 0 would read as "delete immediately".';
COMMENT ON COLUMN public.platform_settings.backup_overwrite_days IS
    'CONTRACTUAL backup-overwrite window in days, counted from the END of data_retention_days '
    '(ToS section 7.3; control-plane public.*). DEFAULT 35. Written into the ToS text at publish '
    'time via the {{BACKUP_OVERWRITE_DAYS}} placeholder. A property of the BACKUP SYSTEM, which is '
    'outside this codebase — nothing here enforces or measures it.';
COMMENT ON COLUMN public.platform_settings.fulfilment_days IS
    'CONTRACTUAL export / deletion-on-request fulfilment window in BUSINESS days (ToS sections 4 '
    'and 8; control-plane public.*). DEFAULT 5. Written into the ToS text at publish time via the '
    '{{FULFILMENT_DAYS}} placeholder. Fulfilled BY HAND by the operator — no scheduler watches it.';

-- ── record WHICH numbers a published ToS version was generated from ──────────────────
-- A published contract version is immutable and its numbers are frozen into its text. Recording
-- the resolved values alongside makes the binding auditable without re-parsing prose: "version
-- 2026-08-12-v1 was published when data_retention_days was 30".
--
-- Guarded on the table existing so this migration is independent of the apply order of 243 in a
-- partially-built control plane (243 < 246, so in a normal run it is always present).
DO $$
BEGIN
    IF to_regclass('public.contract_templates') IS NOT NULL THEN
        ALTER TABLE public.contract_templates
            ADD COLUMN IF NOT EXISTS retention_values JSONB;
        COMMENT ON COLUMN public.contract_templates.retention_values IS
            'The platform retention settings resolved AT PUBLISH TIME for this version, e.g. '
            '{"RETENTION_DAYS": 30, "BACKUP_OVERWRITE_DAYS": 35, "FULFILMENT_DAYS": 5, '
            '"placeholders_substituted": 6}. NULL for a version seeded outside '
            'saas/contracts.py::publish_terms_version (e.g. migration 243''s literal-text seed). '
            'AUDIT ONLY — the authoritative words are body_markdown, and a signed row''s '
            'signed_contracts.contract_snapshot is the frozen copy the customer actually saw.';
    END IF;
END $$;
