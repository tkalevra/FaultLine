-- Migration 303: SaaS IP allow/deny rules — the inbound-IP control plane (BUG-05, P3).
-- (CLOSED-SOURCE SaaS layer.) Date: 2026-10-30
--
-- Productizes inbound source-IP filtering the way migration 144 productized the origin
-- ACL: a managed, operator-editable rule table instead of nothing-but-a-log.
--   * public.saas_ip_rules — rows of (cidr, action). account_id NULL = a GLOBAL rule
--     (applies to every tenant); a non-NULL account_id = that tenant's own rule.
--     Uniqueness is per-scope via two partial indexes (a plain UNIQUE would let duplicate
--     GLOBAL rows through, since NULL != NULL — same reason as allowed_origins).
--   * action is 'allow' | 'deny'. DECISION ORDER (enforced in saas/ip_acl.py, the single
--     seam src/mcp/http_server.py consults): rules from (global ∪ the resolved account)
--     compete; the MOST-SPECIFIC (longest-prefix) matching rule wins; on an exact prefix
--     tie DENY beats ALLOW; when NO rule matches the IP, the default is ALLOW — the same
--     fail-open defense-in-depth posture as the origin ACL (the bearer token + per-tenant
--     schema remain the real boundary; this narrows which source IPs may call at all).
--   * cidr TEXT is canonicalized by the application via Python's ipaddress module
--     (normalize_cidr in saas/ip_acl.py) BEFORE insert — single IPs are accepted and stored
--     as /32 (IPv4) or /128 (IPv6). The DB stores text; parsing happens once at write and
--     once per cached rule-set load, never per request.
--
-- Control-plane public.* (read at request time). NEVER seeded into a tenant schema.
-- Idempotent + forward-only. NO-OP when SAAS_MODE is off (the table sits unread).
-- Read with: saas/ip_acl.py, saas/admin.py (operator CRUD), src/mcp/http_server.py (gate),
-- migrations/144_saas_origin_acl.sql (the sibling this mirrors).

CREATE TABLE IF NOT EXISTS public.saas_ip_rules (
    id          BIGSERIAL PRIMARY KEY,
    account_id  UUID REFERENCES public.accounts(account_id) ON DELETE CASCADE,  -- NULL = GLOBAL
    cidr        TEXT NOT NULL,                     -- canonical via ipaddress (e.g. 203.0.113.0/24)
    action      TEXT NOT NULL CHECK (action IN ('allow', 'deny')),
    note        TEXT,                              -- free-form operator remark (audit hint)
    created_by  TEXT,                              -- 'operator:<u>' | 'operator:global'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-tenant uniqueness (one row per (account, cidr, action)).
CREATE UNIQUE INDEX IF NOT EXISTS uq_saas_ip_rules_tenant
    ON public.saas_ip_rules (account_id, cidr, action) WHERE account_id IS NOT NULL;
-- Global uniqueness (one row per (cidr, action) among the account_id-NULL entries).
CREATE UNIQUE INDEX IF NOT EXISTS uq_saas_ip_rules_global
    ON public.saas_ip_rules (cidr, action) WHERE account_id IS NULL;
-- Fast per-tenant + global scans.
CREATE INDEX IF NOT EXISTS idx_saas_ip_rules_account
    ON public.saas_ip_rules (account_id);
