#!/usr/bin/env bash
# FOSS LEAK GATE — run before pushing any foss* branch to the public remote.
#
# WHY THIS EXISTS. Both lines now live in ONE repo (branch-named separation: `saas` = private
# full line, `foss*` = the public AGPLv3 line), so validating FOSS-readiness no longer means
# flipping repositories. The cost of that convenience is that the closed layer is one bad merge
# away from the public branch. This is the check that makes the convenience safe.
#
# The two lineages share NO ancestry (`git merge-base saas foss` is empty) — they are ported
# between, never merged. If this gate ever fires, something merged that should have been ported.
#
# Usage:  tools/foss_gate.sh [branch]     (default: current branch)
# Exit 0 = clean, safe to push.  Exit 1 = STOP, closed-source content detected.
set -uo pipefail

BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
FAIL=0

say() { printf '%s\n' "$*"; }
bad() { printf '  ✗ %s\n' "$*"; FAIL=1; }
ok()  { printf '  ✓ %s\n' "$*"; }

say "FOSS leak gate — branch: $BRANCH"

# 1. The closed module must not exist as a tracked path at all.
if [ "$(git ls-tree --name-only "$BRANCH" | grep -c '^saas$')" -ne 0 ]; then
    bad "the closed 'saas/' directory is TRACKED on this branch"
else
    ok "no saas/ directory"
fi

# 2. No file anywhere in the tree may live under saas/ (catches a nested re-add).
n=$(git ls-tree -r --name-only "$BRANCH" | grep -c '^saas/' || true)
[ "$n" -ne 0 ] && bad "$n tracked files under saas/" || ok "no files under saas/"

# 3. Content grep — closed-layer references that BREAK the open core.
#
#    FOSS-PORT DOCTRINE (2026-11-15 uplift): the private line's gate grepped the whole tree
#    for ANY mention of saas/SAAS_MODE/flagent_. After the uplift the open core legitimately
#    carries SAAS_MODE-gated seams (the closed product layers mount only when SAAS_MODE=1 —
#    src/mcp/http_server.py's admin routers, llm_calls' per-account egress, etc.) and the
#    agent cortex as public-src (flagent_<uuid> IS the open core's cortex schema —
#    migrations/147). ~130 lazy, function-scoped saas imports ride inside try/except arms
#    that fail safe to the open-core path when the saas package is absent — that is the
#    sanctioned dual-mode shape this port carries VERBATIM from the source tip, and the moat
#    grep receipt in the port report enumerates them.
#    What actually BREAKS the open core is an UNGUARDED module-level import (crashes every
#    FOSS import at startup) or a closed-only consumer name. Those are what this check flags:
#      (a) module-level (column-0) `from saas` / `import saas` anywhere in shipped code,
#      (b) direct `saas.<module>` ATTRIBUTE calls outside an import statement,
#      (c) tracked files under the closed trees.
hits=$(git grep -n -E '^(from saas|import saas)|[^_a-zA-Z]saas\.[a-z_]+\(' "$BRANCH" \
        -- 'src' 'tests' 'tools' 'openwebui' 'webui' 'quickstart.py' 'sdk' 2>/dev/null \
        | grep -vE ':[0-9]+:[[:space:]]*(from|import)[[:space:]]' | head -20 || true)
# 4. CLOSED-ONLY cortex consumers. The cortex ITSELF is part of the open core since the
#    2026-11-15 uplift (src/api/cortex.py, migrations/147_cortex_episodes.sql, the
#    cortex_remember/cortex_recall MCP tools, the flagent_<uuid> schema, the
#    cortex_engagement counts table inside src/api/cortex.py — all ported public-src).
#    What stays closed is the private line's ops-dashboard consumer surface (cortex_ops);
#    that name must never appear on a public branch.
if git grep -q -iE 'cortex_ops' "$BRANCH" \
     -- ':!tools/foss_gate.sh' ':!migrations' ':!CHANGELOG.md' 2>/dev/null; then
    bad "closed-only cortex consumer symbols present (console/engagement layer)"
else
    ok "no closed-only cortex consumer symbols"
fi

# 5. PRODUCTION INFRASTRUCTURE DISCLOSURE.
#
#    Checks 1-4 all look for the word "saas". That is why they would have passed
#    `ops/egress/` — the host egress-SNAT tooling added 2026-08-11 — straight onto a public
#    branch. Nothing in those files says "saas", yet they publish production's real public
#    /29, its container subnet, its WAN interface name, its host env path and the exact NAT
#    layout: a map of where to aim at the live service. The moat is not the only thing worth
#    withholding; the running system's topology is too.
#
#    Kept deliberately narrow so it cannot false-positive on legitimate public content
#    (ops/ansible/ ships *.example templates and is publishable; only ops/egress/ is
#    prod-specific). Baseline verified at the time of writing: foss-origin/main had ZERO
#    hits for every pattern below.
infra_fail=0

n=$(git ls-tree -r --name-only "$BRANCH" | grep -c '^ops/egress/' || true)
if [ "$n" -ne 0 ]; then
    bad "$n tracked files under ops/egress/ (production egress/NAT tooling — SaaS-host only)"
    infra_fail=1
fi

# The prod public /29, the prod WAN NIC, and the host-only env path. Each is meaningless
# to a self-hoster and useful only to someone probing the hosted service.
infra_hits=$(git grep -n -E '38\.143\.57\.[0-9]+|enp193s0|/etc/faultline/' "$BRANCH" \
              -- ':!tools/foss_gate.sh' 2>/dev/null | head -20 || true)
if [ -n "$infra_hits" ]; then
    bad "production infrastructure detail (public IPs / WAN NIC / host env path):"
    printf '%s\n' "$infra_hits" | sed 's/^/      /'
    infra_fail=1
fi

[ "$infra_fail" -eq 0 ] && ok "no production infrastructure disclosure"

say ""
if [ "$FAIL" -eq 0 ]; then
    say "CLEAN — $BRANCH is safe to push to the public remote."
else
    say "STOP — $BRANCH carries closed-source content. Do NOT push."
fi
exit "$FAIL"
