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

# 3. Content grep. A SAAS_MODE-gated seam is INERT with the flag off but still leaks moat
#    detail (naming saas.* helpers, SaaS migrations, the Seats page) — that is a reason to
#    WITHHOLD the change, not to ship it because it is "safe".
hits=$(git grep -n -iE 'from saas|import saas|saas\.[a-z_]+\(|SAAS_MODE|flagent_|flagent-' "$BRANCH" \
        -- ':!tools/foss_gate.sh' ':!tests/test_boot_migrations_foss_corpus.py' 2>/dev/null | head -20 || true)
if [ -n "$hits" ]; then
    bad "closed-layer references found:"
    printf '%s\n' "$hits" | sed 's/^/      /'
else
    ok "no saas/cortex/SAAS_MODE references"
fi

# 4. The agent cortex is SaaS-only by design (owner-stated). Its schema/collection prefixes
#    must never appear on a public branch.
if git grep -q -iE 'cortex_episodes|cortex_engagement|iremember|irecall' "$BRANCH" \
     -- ':!tools/foss_gate.sh' 2>/dev/null; then
    bad "agent-cortex symbols present (cortex is a SaaS-only offering)"
else
    ok "no agent-cortex symbols"
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
