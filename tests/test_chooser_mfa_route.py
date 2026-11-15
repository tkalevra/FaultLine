"""Chooser MFA-route pin (BUGS/same-page-login-20261102; owner hit 2026-11-02).

DEFECT (live, one-click chooser): unified_grant_complete answers an MFA challenge with
realm="mfa" + realm_source="<origin realm>", but the chooser's completion handler routed
by realm === 'tenant' — false on MFA outcomes — so a TENANT MFA challenge was handed to
the SEAT console page (user.html#oauth=mfa:<tenant-token>), which cannot consume it:
the owner's Bitwarden second-factor prompt never appeared ("MFA skipped").

FIX: route by realm_source. This module drives the REAL access.html + landing.js bytes
in jsdom (fetch stubbed, navigation attempts captured) and pins: tenant door MFA ->
tenant.html#oauth=mfa:<token>; seat door MFA -> user.html#oauth=mfa:<token>.
"""
import json
import os
import re
import shutil
import subprocess
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "dashboard" / "render_chooser_mfa_route.js"
LANDING_JS = ROOT / "saas" / "landing" / "landing.js"
INSTALL_HINT = "npm install --prefix tests/dashboard"

_MODULE_MISSING = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    if not HARNESS.exists():
        pytest.skip(f"render harness missing at {HARNESS.relative_to(ROOT)}")
    env = dict(os.environ)
    roots = [str(HARNESS.parent / "node_modules"), str(ROOT / "node_modules")]
    if env.get("NODE_PATH"):
        roots.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(roots)
    try:
        out = subprocess.run([node, str(HARNESS)], capture_output=True, text=True,
                             timeout=180, cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:
        pytest.fail("render harness timed out after 180s")
    if out.returncode != 0:
        missing = _MODULE_MISSING.search(out.stderr or "")
        if missing:
            pytest.skip(f"node module {missing.group(1)!r} is not installed - run:  {INSTALL_HINT}")
        pytest.fail("render harness exited %d:\n%s" % (out.returncode, (out.stderr or "")[-3000:]))
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail("render harness emitted non-JSON stdout: %s" % exc)


def test_one_click_wiring_and_mfa_branch(report):
    steps = report["steps"]
    assert steps["flavour_count"] >= 2, "chooser doors missing"
    assert steps["tenant_complete_destination"] == "tenant", "tenant door must complete with destination=tenant"
    assert steps["tenant_mfa_navigates"], "mfa_required branch must navigate (handoff to the realm console)"
    assert steps["seat_mfa_navigates"], "seat door mfa_required branch must navigate"


def test_routing_uses_realm_source_not_realm():
    """THE regression guard: jsdom cannot observe the navigation target, so the
    URL-correctness (tenant.html vs user.html by realm_source) is pinned statically
    against the real routing line."""
    src = LANDING_JS.read_text(encoding="utf-8")
    m = re.search(r"mfa_required[\s\S]{0,600}?var page = ([^;]+);", src)
    assert m, "chooser mfa_required routing block not found"
    assert "realm_source" in m.group(1), "chooser must route MFA by realm_source, never by realm"
