#!/usr/bin/env python3
"""PostToolUse hook — auto-capture FAILED Bash commands into the agent cortex.

Fires after every Bash tool call. On a high-signal FAILURE it writes a
`failed_command` note to my operational memory (the firewalled per-seat
`flagent_<seat>` cortex) via POST /iremember. Deduped by /iremember's
UNIQUE(category, md5(note)), so a recurring failure bumps observation_count
and HARDENS into a rule (>=3) — exactly the ":1234 keeps failing" case.

Design rails:
  - CONSERVATIVE: only fires on curated high-signal failure markers, so it
    doesn't flood the cortex on every non-zero exit / expected error.
  - FAIL-SAFE + FAST: never blocks or errors the command (always exit 0);
    no-ops instantly on success; bounded curl; silent if the cortex is down.
  - NO LOOP / NO NOISE: skips its own cortex/curl-to-8002 traffic.
  - The note is COMMAND-stable (not output-stable) so repeats dedup+harden;
    the fix/lesson is still added by a deliberate /iremember. Auto-capture
    surfaces WHAT keeps failing; I supply WHY/how-to-fix.
Seat: env CORTEX_SEAT, else the dev-agent seat.
"""
import sys, json, os, re, subprocess


def _cfg(key, default=""):
    """Env first, then the project's .claude/settings.local.json `env` block — so the hook
    works with ZERO per-user effort whether or not Claude Code propagates env to the subprocess."""
    v = os.environ.get(key)
    if v:
        return v
    try:
        _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.local.json")
        return (json.load(open(_p)).get("env") or {}).get(key, default)
    except Exception:
        return default


MCP = _cfg("CORTEX_MCP", "http://localhost:8002")
TOKEN = _cfg("CORTEX_TOKEN", "")   # flu_/flk_ bearer → resolves the seat server-side (per-seat, zero-config)
SEAT = _cfg("CORTEX_SEAT", "")     # ONLY the token-less local/open dev case needs a seat id

# High-signal failure markers — a real failure, not just any stderr chatter.
FAIL_MARKERS = (
    "command not found", "no such file or directory", "fatal:", "permission denied",
    "traceback (most recent call last)", "could not resolve host", "connection refused",
    "name or service not known", "cannot access", "no such container", "not a git repository",
    "merge conflict", "syntaxerror", "modulenotfounderror", "importerror",
    "exit code: 1", "exit code: 2", "exit code: 127", "operation not permitted",
)

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    ti = data.get("tool_input") or {}
    cmd = (ti.get("command") or "").strip()
    if not cmd:
        return
    # Don't capture our own cortex/curl traffic (avoid noise + any loop).
    low_cmd = cmd.lower()
    if "/iremember" in low_cmd or "/irecall" in low_cmd or "8002" in cmd or "cortex" in low_cmd:
        return
    # Normalize the tool response to text + an explicit error flag.
    resp = data.get("tool_response")
    if isinstance(resp, dict):
        text = " ".join(str(resp.get(k, "")) for k in ("stdout", "stderr", "output", "error", "content"))
        is_err = bool(resp.get("is_error") or resp.get("isError") or resp.get("error"))
    else:
        text = str(resp or "")
        is_err = False
    low = text.lower()
    matched = next((m for m in FAIL_MARKERS if m in low), None)
    if not is_err and not matched:
        return  # success / benign → fast no-op
    tool = re.split(r"\s+", cmd)[0].rsplit("/", 1)[-1] or "cmd"
    reason = matched or "error"
    note = f"[{tool}] command failed ({reason}): {cmd[:150]}"
    tags = sorted({tool, "auto-capture", "failed-command"})
    body = {"text": note, "category": "failed_command", "tags": tags}
    headers = ["-H", "Content-Type: application/json"]
    if TOKEN:
        headers += ["-H", f"Authorization: Bearer {TOKEN}"]   # flu_/flk_ resolves the seat
    else:
        body["user_id"] = SEAT
        headers += ["-H", f"X-OpenWebUI-User-Id: {SEAT}"]
    try:
        subprocess.run(
            ["curl", "-sS", "-m", "5", "-X", "POST", f"{MCP}/iremember", *headers, "-d", json.dumps(body)],
            capture_output=True, timeout=8,
        )
    except Exception:
        pass  # cortex down / offline → silently skip; never block the session

if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)  # ALWAYS succeed — a capture failure must never break a tool call
