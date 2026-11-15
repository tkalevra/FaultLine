"""The Claude-Code cortex hooks — the three CONFIRMED defects, pinned.

This is LOCAL TOOLING, not the product (the product-side answer is the model-independent
experience lane — a hook reaches one client and delivers nothing to Mistral/Qwen/OpenWebUI).
It is pinned anyway because each defect cost real time and each is silent when it regresses.
"""
import importlib.util
import os

import pytest

_HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".claude", "hooks")


def _load(name):
    path = os.path.join(_HOOKS, f"{name}.py")
    if not os.path.exists(path):
        pytest.skip(f"{name}.py not present")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load("cortex_surface")
cw = _load("cortex_warm")


# ── DEFECT (b): selection happened BEFORE relevance ───────────────────────────────────────

_AUDITED_MEMORY = (
    "My own operational notes for this:\n"
    "- [correction] [rule] USER CORRECTED me - the LME fix-pipeline resume/heartbeat must be "
    "restarted by hand after a reboot.\n"
    "- [howto] STOP - do not run pytest directly. Run EVERY test through "
    "`python3 tools/fltest.py --bug <REF> --test tests/test_x.py` so the run is stamped.\n"
)
_AUDITED_QUERY = "python3 pytest tests"


def test_the_right_note_survives_an_irrelevant_hardened_rule():
    """THE AUDITED CASE. `_top_note()` preferred any `[rule]` bullet REGARDLESS of relevance and
    only then judged it — so the exactly-correct note, retrieved and ranked FIRST by the server,
    was discarded for an unrelated `[rule]` with ZERO overlap, which then failed the floor and
    the hook emitted nothing. Score ALL, then pick."""
    note, overlap = cs._best_note(_AUDITED_QUERY, _AUDITED_MEMORY)
    assert "fltest.py" in note, note
    assert overlap >= cs._OVERLAP_FLOOR

    # and the old order, reconstructed, is exactly the failure — kept here so the regression is
    # legible rather than a bare assertion.
    bullets = cs._bullets(_AUDITED_MEMORY)
    old_choice = next(b for b in bullets if "[rule]" in b)
    assert not cs._passes_floor(_AUDITED_QUERY, old_choice)


def test_rule_hardening_only_breaks_ties_never_admits():
    """A hardened note with a WEAKER overlap must not outrank a stronger plain one."""
    memory = ("x:\n"
              "- [gotcha] [rule] docker notes are here\n"
              "- [howto] docker compose production deploy recipe lives at /opt\n")
    note, _ = cs._best_note("docker compose production deploy", memory)
    assert "recipe" in note


def test_autocaptured_failed_command_needs_a_much_higher_bar():
    """DEFECT (d), CONFIRMED LIVE 2026-08-01 — three misfires in one evening, this one measured
    verbatim in front of a real command. An auto-captured `[failed_command]` note IS a raw shell
    command, so its token pool is PLUMBING and it collides structurally with any other command.
    Here every shared token ({done, grep, tail}) is plumbing and it still cleared the soft floor
    of 3 and was surfaced. Keyed on the fixed category enum — the same tier the backend already
    treats as weakest (the only decay-eligible category)."""
    note = ("[failed_command] [tail] command failed: tail -6 /tmp/_arms.log 2>/dev/null; echo; "
            "docker logs fl-probe 2>&1 | grep -E \"seg|done\" | tail -6 [unverified]")
    q = cs._query_from_tool("Bash", {"command":
        'until grep -q "PASS" /tmp/fw_run.log 2>/dev/null; do sleep 5; done; tail -14 /tmp/x.log'})
    overlap = cs._salient_set(q) & cs._salient_set(note)
    assert len(overlap) >= cs._OVERLAP_FLOOR, "the collision itself must still be reproduced"
    assert not cs._passes_floor(q, note), f"auto-capture noise surfaced on {sorted(overlap)}"


def test_the_curated_notes_that_must_still_fire_do():
    """The floor change must not buy silence by suppressing the real hits — including the
    ':8080 not :1234' case the hook was built for."""
    for cmd, note in [
        ("python3 -m pytest tests/ -q",
         "[howto] STOP - do not run pytest directly. Run EVERY test through "
         "`python3 tools/fltest.py --bug X --test tests/test_x.py`."),
        ("sudo docker compose -p faultline -f /opt/faultline/docker-compose.yml up -d",
         "[howto] PROD deploy: build context /home/deploy/faultline-src, then sudo docker "
         "compose -p faultline -f /opt/faultline/docker-compose.yml up -d --force-recreate."),
        ("curl http://192.168.40.20:1234/v1/models",
         "[gotcha] [rule] the model server is on 192.168.40.20:8080, not :1234."),
    ]:
        q = cs._query_from_tool("Bash", {"command": cmd})
        assert cs._passes_floor(q, note), note[:60]


def test_a_hardened_failed_command_escapes_the_autocapture_class():
    """Repetition is how a note earns its way out of the weakest tier — a `[rule]` is not
    incidental noise, so it is judged by the hardened floor like any other rule."""
    note = "[failed_command] [rule] never point the deploy at the pooled DSN [unverified]"
    q = cs._query_from_tool("Bash", {"command": "deploy --dsn pooled"})
    assert cs._passes_floor(q, note)


def test_below_floor_stays_silent():
    memory = "x:\n- [howto] something completely unrelated to the command\n"
    assert cs._best_note("psql truncate tenant", memory) == ("", 0)


def test_no_bullets_is_silence_not_a_crash():
    assert cs._best_note("anything", "I have no operational notes on that yet.") == ("", 0)
    assert cs._best_note("anything", "") == ("", 0)


# ── DEFECT (c)/§1.3: an automatic lane must not re-rank what it reads ─────────────────────

def test_hook_requests_a_non_bumping_read():
    """The surface hook fires on every Bash/Edit/Write. Reading the cortex bumps
    hit_count/last_seen_at while candidates are ORDER BY last_seen_at DESC, so an automatic
    lane that bumps eats its own candidate window."""
    import inspect
    src = inspect.getsource(cs.main)
    assert '"bump": False' in src or "'bump': False" in src


def test_warmup_never_bumps_either():
    import inspect
    assert '"bump": False' in inspect.getsource(cw.main)


# ── DEFECT (a): the timeout budget sat BELOW the measured cold latency ────────────────────

def test_two_budgets_cold_is_short_warm_is_generous(tmp_path, monkeypatch):
    """Cold latency measured at 8.0-12.3s, warm at 0.10-0.20s; the old flat `curl -m 4` sat
    between them, so a session's FIRST call — always cold — always timed out. The fix is not a
    bigger blocking budget: cold ⇒ SHORT budget and silence (cortex_warm.py absorbs the cold
    latency off the critical path); warm ⇒ a generous ceiling that is never actually spent."""
    monkeypatch.setattr(cw.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(cs.tempfile, "gettempdir", lambda: str(tmp_path))
    sid = "s1"
    cold_m, cold_proc = cs._budget(sid)
    open(cw.warm_marker_path(sid), "w").close()
    warm_m, warm_proc = cs._budget(sid)
    assert cold_m < warm_m
    assert cold_m <= 2.0, "the acting-moment call must not pay the cold latency"
    assert warm_m >= 6.0
    assert cold_proc > cold_m and warm_proc > warm_m, "subprocess timeout must exceed curl -m"


def test_warm_marker_path_is_shared_by_both_hooks(tmp_path, monkeypatch):
    """One definition of 'warm', imported — not two string literals that can drift."""
    import inspect
    assert "warm_marker_path" in inspect.getsource(cs._budget)


# ── dedupe: a weak early hit must not permanently suppress a later strong one ─────────────

def test_dedupe_allows_a_materially_stronger_resurface(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.tempfile, "gettempdir", lambda: str(tmp_path))
    sid, fp = "s2", "abc123"
    assert cs._already_surfaced(sid, fp, 1) is False
    cs._record_surfaced(sid, fp, 1)
    assert cs._already_surfaced(sid, fp, 1) is True          # same strength → stay quiet
    assert cs._already_surfaced(sid, fp, 2) is True          # marginally better → stay quiet
    assert cs._already_surfaced(sid, fp, 3) is False         # materially better → re-surface


def test_dedupe_reads_legacy_scoreless_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.tempfile, "gettempdir", lambda: str(tmp_path))
    sid, fp = "s3", "deadbeef"
    with open(cs._state_path(sid), "w") as f:
        f.write(fp + "\n")                                   # pre-fix state file
    assert cs._already_surfaced(sid, fp, 0) is True
    assert cs._already_surfaced(sid, fp, 5) is False


# ── config resolution ─────────────────────────────────────────────────────────────────────

def test_explicitly_empty_env_does_not_fall_through_to_the_project_token(monkeypatch):
    """An explicitly-cleared CORTEX_TOKEN must mean 'no token' — falling through to the
    settings file silently points the project's real bearer at whatever host is under test."""
    monkeypatch.setenv("CORTEX_TOKEN", "")
    assert cs._cfg("CORTEX_TOKEN", "fallback") == ""
