#!/usr/bin/env python3
"""PreToolUse hook — surface a relevant cortex gotcha BEFORE the tool runs.

"Have my back before I act." Before a Bash/Edit/Write tool executes, this hook
queries the agent cortex (POST /irecall) for an operational note relevant to the
command/args and, on a HIGH-relevance hit, feeds the single top note back to the
agent as context — so the ":8080 not :1234" note appears BEFORE the endpoint is
fumbled, not after it fails.

Mechanism (verified against Claude Code 2.1.205):
  - PreToolUse stdin carries: tool_name, tool_input, transcript_path, cwd, ...
  - Exit 0 + JSON on stdout is parsed. We emit:
        {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                 "additionalContext": "<the note>"}}
    additionalContext is delivered to the agent WITHOUT blocking the tool. We
    deliberately OMIT permissionDecision so the normal permission flow is
    untouched (we are informing, not gating).

Design rails (match cortex_capture.py idiom):
  - DETERMINISTIC: /irecall is keyword+tag top-K, no cosine. We derive the query
    from the command + its salient args and surface at most ONE note.
  - SIGNAL NOT NOISE — RELEVANCE FLOOR: /irecall returns SOMETHING for almost any
    token overlap, so a bare top-K hit is NOT enough. We require the chosen note
    to share >= _OVERLAP_FLOOR (2) SALIENT tokens with the query (case-insensitive,
    minus _STOP + very-short tokens) OR be a hardened [rule]/[gotcha] note whose
    trigger clearly appears in the command (>= 1 shared salient token). Below the
    floor → surface NOTHING. Deterministic, no LLM. This kills the "vector-memories
    note surfaces before a tenant.html edit on one shared token" misfire.
  - SIGNAL NOT NOISE — PER-SESSION DEDUPE: never surface the SAME note twice in one
    session (ambient recall that repeats itself becomes wallpaper). We fingerprint
    the note text (sha1) and keep a per-session state file keyed on the stdin
    session_id (/tmp/cortex_surfaced_<session_id>.txt). Already-recorded → skip. A
    fresh session_id starts fresh automatically. Any file error → behave as
    not-yet-surfaced (never crash, never block, never suppress a real first hit).
  - FAST + FAIL-SAFE: bounded curl; ANY error → surface nothing, exit 0. A hook
    failure NEVER blocks or delays the tool beyond the short timeout.
  - NO LOOP / NO NOISE: skips its own cortex/8002 traffic.
Seat: env CORTEX_SEAT, else the shared dev-agent seat (same as cortex_capture).
"""
import sys, json, os, re, subprocess, hashlib, tempfile


def _cfg(key, default=""):
    """Env first, then the project's .claude/settings.local.json `env` block. So the hook
    works with ZERO per-user effort whether or not Claude Code propagates the env block to
    the subprocess (it did not, which is half of why this was silently dead)."""
    v = os.environ.get(key)
    if v is not None:
        # An EXPLICITLY EMPTY env var means "explicitly none" and must NOT fall through to the
        # settings file — otherwise a token-less local/dev run silently picks up the project's
        # real bearer and points it at the wrong host.
        return v
    try:
        _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.local.json")
        return (json.load(open(_p)).get("env") or {}).get(key, default)
    except Exception:
        return default


MCP = _cfg("CORTEX_MCP", "http://localhost:8002")
TOKEN = _cfg("CORTEX_TOKEN", "")   # flu_/flk_ bearer → resolves the seat server-side (per-seat, zero-config)
SEAT = _cfg("CORTEX_SEAT", "")     # ONLY the token-less local/open dev case needs a seat id

_STOP = {"the", "and", "for", "with", "sudo", "then", "this", "that", "from",
         "into", "true", "false", "null", "echo", "cat", "ls", "cd"}

# Relevance floor: minimum SALIENT-token overlap between the query and the chosen
# note before we surface it.
#   - HARDENED [rule]/[gotcha] notes are curated, terse, deliberately trigger-shaped
#     (the deploy/psql/grep operational MEAT). Their trigger appearing in the command
#     IS the signal → fast-path at 1 shared salient token.
#   - SOFT notes ([correction] etc.) are prose-heavy conversational captures whose
#     large token pool overlaps INCIDENTALLY on generic project tokens (observed live:
#     an edit of saas/dashboard/tenant.html sharing {dashboard, tenant} with a stale
#     tenant-admin correction — 2 tokens of pure location noise). Started at N=2 per
#     brief; tuned to 3 because 2 admitted exactly that incidental project-structure
#     overlap. A soft note now fires only on 3+ genuinely-shared tokens = real topical
#     relevance, not ambient path collision.
#   - AUTO-CAPTURED [failed_command] notes are a THIRD class and the noisiest of all: the note
#     text IS a raw shell command, so its token pool is shell PLUMBING and it collides with any
#     other command structurally, not topically. CONFIRMED live (2026-08-01), three misfires in
#     one evening — an ansible failure note in front of a Python `ast.parse`, an unrelated `cd`
#     failure note, and this one, measured verbatim:
#         command: until grep -q "^=== (PASS|FAIL)" /tmp/fw_run.log ...; tail -14 ...
#         note   : [failed_command] tail -6 /tmp/_arms.log ...; docker logs fl-probe | grep -E ...
#         overlap = 3 {done, grep, tail}  -> cleared the soft floor of 3 and was SURFACED
#     Every shared token is plumbing. Keyed on the fixed CATEGORY enum — not a word list — and
#     the same enum the backend already singles out as the weakest tier (`failed_command` is the
#     ONLY decay-eligible category in cortex.py). It must clear a materially higher bar.
_OVERLAP_FLOOR = 3
_HARDENED_FLOOR = 1
_AUTOCAPTURE_FLOOR = 5


def _salient_set(text):
    """Deterministic salient-token set for overlap scoring. Full IPv4 literals kept
    whole (they are strong triggers, e.g. ':8080 not :1234' hosts); everything else
    reduced to lowercase word/number runs of >= 4 chars (so '.', '-', '_', '/' split
    'tenant.html' → {tenant, html}), minus _STOP + very-short tokens. Applied to BOTH
    the query and the note so the comparison is symmetric."""
    if not text:
        return set()
    low = text.lower()
    toks = set(re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", low))     # full IPv4 kept whole
    for w in re.findall(r"[a-z0-9]{4,}", low):                  # >=4 char word/number runs
        if w not in _STOP:
            toks.add(w)
    return toks


def _query_from_tool(tool_name, tool_input):
    """Build an irecall query from the command + salient args (ports, IPs,
    hosts, path basenames, flags, content words). Returns "" when too thin."""
    ti = tool_input or {}
    if tool_name == "Bash":
        raw = (ti.get("command") or "").strip()
    else:  # Edit / Write / NotebookEdit
        raw = (ti.get("file_path") or ti.get("notebook_path") or "").strip()
    if not raw:
        return ""
    toks = []
    # First real token (the base command / verb) is a strong trigger.
    first = re.split(r"\s+", raw)[0].rsplit("/", 1)[-1]
    if first:
        toks.append(first)
    toks += re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", raw)      # IPv4
    toks += re.findall(r"\b\d{2,5}\b", raw)                      # ports/numbers
    toks += re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{3,}", raw)       # words / hosts / files
    seen, out = set(), []
    for t in toks:
        tl = t.lower()[:40]
        if tl and tl not in _STOP and tl not in seen:
            seen.add(tl)
            out.append(tl)
    return " ".join(out[:16])


def _bullets(memory):
    """The irecall prose split back into its bullets ([] when there is no real hit)."""
    if not memory or "\n- " not in memory:
        return []  # the "no operational notes" sentinel has no bullets
    return [b.strip() for b in memory.split("\n- ")[1:] if b.strip()]


def _passes_floor(query, note):
    """RELEVANCE FLOOR: does `note` share enough salient tokens with `query` to be
    worth surfacing? Hardened [rule]/[gotcha] notes clear at _HARDENED_FLOOR (their
    trigger appearing in the command is the signal); everything else must reach
    _OVERLAP_FLOOR. Deterministic; no LLM/cosine."""
    overlap = _salient_set(query) & _salient_set(note)
    if "[failed_command]" in note and "[rule]" not in note:
        # Auto-captured raw commands collide on shell plumbing, not on topic (see above).
        # A HARDENED one has earned its way out of this class by repetition.
        return len(overlap) >= _AUTOCAPTURE_FLOOR
    hardened = ("[rule]" in note) or ("[gotcha]" in note)
    return len(overlap) >= (_HARDENED_FLOOR if hardened else _OVERLAP_FLOOR)


def _best_note(query, memory):
    """SCORE EVERY CANDIDATE, THEN PICK. Returns (note, overlap) or ("", 0).

    ⚠️ THIS ORDER IS THE FIX. The previous `_top_note()` preferred any `[rule]`-tagged bullet
    REGARDLESS OF RELEVANCE and only THEN judged that one against the floor — so a single
    unrelated hardened note could veto the whole turn. Proven on the pytest case:

        query = "python3 pytest tests"
          rank0  overlap=3  [howto] STOP - do not run `pytest` directly. Run EVERY test
                            through `python3 tools/fltest.py ...`   <- would PASS the floor
          chosen           [correction] [rule] USER CORRECTED me - "LME fix-pipeline ..."
          chosen_overlap = []   passes = False   -> HOOK EMITTED NOTHING

    The exactly-correct note was retrieved and ranked FIRST by the server, and the client threw
    it away for a `[rule]` with ZERO token overlap. Now: every bullet is scored, only survivors
    of the floor compete, and `[rule]`/`[gotcha]` hardening is a TIE-BREAK — never an admission
    ticket. Server rank breaks a remaining tie (the server already ranked by its own hybrid
    score, so its order is real information, not noise)."""
    best_key, best_note, best_ov = None, "", 0
    for rank, note in enumerate(_bullets(memory)):
        if not _passes_floor(query, note):
            continue
        overlap = len(_salient_set(query) & _salient_set(note))
        hardened = 1 if (("[rule]" in note) or ("[gotcha]" in note)) else 0
        key = (overlap, hardened, -rank)
        if best_key is None or key > best_key:
            best_key, best_note, best_ov = key, note, overlap
    return best_note, best_ov


def _fingerprint(note):
    return hashlib.sha1(note.encode("utf-8", "replace")).hexdigest()[:16]


def _state_path(session_id):
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", (session_id or "nosession"))[:80]
    return os.path.join(tempfile.gettempdir(), f"cortex_surfaced_{sid}.txt")


# A note already surfaced weakly may re-surface if a LATER command matches it MATERIALLY better.
# Without this, one incidental early hit permanently suppresses the note at the moment it is
# actually needed — dedupe silencing the exact thing dedupe exists to make audible.
_RESURFACE_DELTA = 2


def _already_surfaced(session_id, fp, overlap=0):
    """PER-SESSION DEDUPE read side, SCORE-AWARE. State lines are `<fp> <best_overlap>`.
    Suppress only when this hit is NOT materially stronger than the best previous one.
    Fail-safe: any file error → treat as NOT yet surfaced (never suppress a real first hit on a
    transient FS error). Legacy score-less lines read as score 0 and can be superseded."""
    try:
        with open(_state_path(session_id), "r") as f:
            for line in f:
                parts = line.split()
                if not parts or parts[0] != fp:
                    continue
                try:
                    prev = int(parts[1]) if len(parts) > 1 else 0
                except ValueError:
                    prev = 0
                return overlap < prev + _RESURFACE_DELTA
        return False
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _record_surfaced(session_id, fp, overlap=0):
    """PER-SESSION DEDUPE write side. Best-effort append; any error is swallowed so a
    write failure degrades to 'may re-surface', never a crash/block."""
    try:
        with open(_state_path(session_id), "a") as f:
            f.write(f"{fp} {int(overlap)}\n")
    except Exception:
        pass


# ── TIMEOUT BUDGET — see cortex_warm.py for the measurement that drives these numbers ──────
# Cortex latency: 8.0-12.3s COLD (first call of a session), 0.10-0.20s WARM. The old flat
# `curl -m 4` sat between the two: below the cold latency (so the FIRST call of every session
# — the one that matters — always timed out and emitted nothing) and far above the warm one.
# Two budgets instead of one bad compromise:
#   * WARM (SessionStart warm-up completed, marker present): 6s ceiling. Typical spend 0.1-0.2s.
#     The ceiling exists only for a slow-but-alive cortex; it is not the expected cost.
#   * COLD (no marker yet — the warm-up is still running or the cortex is down): 1.5s. We
#     deliberately do NOT pay the cold latency in front of a tool call. Silence is the correct
#     outcome; cortex_warm.py is already absorbing that cost off the critical path.
_BUDGET_WARM = float(_cfg("CORTEX_SURFACE_TIMEOUT_WARM", "6") or 6)
_BUDGET_COLD = float(_cfg("CORTEX_SURFACE_TIMEOUT_COLD", "1.5") or 1.5)


def _budget(session_id):
    """(curl -m seconds, subprocess timeout seconds) for this call."""
    warm = False
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from cortex_warm import warm_marker_path
        warm = os.path.exists(warm_marker_path(session_id))
    except Exception:
        warm = False
    m = _BUDGET_WARM if warm else _BUDGET_COLD
    return m, m + 2.0


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tool = data.get("tool_name") or ""
    if tool not in ("Bash", "Edit", "Write", "NotebookEdit"):
        return
    ti = data.get("tool_input") or {}
    raw = (ti.get("command") or ti.get("file_path") or "").lower()
    # Don't react to our own cortex/curl traffic (avoid noise + any loop).
    if any(s in raw for s in ("/iremember", "/irecall", "8002", "cortex")):
        return
    query = _query_from_tool(tool, ti)
    if len(query.split()) < 1:
        return

    # Per-seat + ZERO-CONFIG: the token resolves its OWN seat server-side, so send NO user_id
    # — a mismatched user_id 403s "tenant spoof" (the exact silent-failure bug). A seat id is
    # used ONLY in the token-less local/open dev case.
    #
    # bump=false — THIS IS AN AUTOMATIC LANE. Reading the cortex re-ranks it (`hit_count` +
    # `last_seen_at`, and the candidate field is `ORDER BY last_seen_at DESC LIMIT 500`), so a
    # hook that fires on every Bash/Edit/Write and bumps keeps whatever it surfaces permanently
    # at the head of the window — a lane eating its own tail. A DELIBERATE /irecall (the agent
    # asking) still bumps; that one is a real relevance signal. An older server that does not
    # know the field ignores it and simply behaves as it does today.
    body = {"query": query, "bump": False}
    if not TOKEN:
        body["user_id"] = SEAT
    headers = ["-H", "Content-Type: application/json"]
    if TOKEN:
        headers += ["-H", f"Authorization: Bearer {TOKEN}"]
    elif SEAT:
        headers += ["-H", f"X-OpenWebUI-User-Id: {SEAT}"]
    session_id = data.get("session_id") or ""
    curl_m, proc_timeout = _budget(session_id)
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-m", str(curl_m), "-X", "POST", f"{MCP}/irecall",
             *headers, "-d", json.dumps(body)],
            capture_output=True, timeout=proc_timeout, text=True,
        )
        resp = json.loads(proc.stdout or "{}")
    except Exception:
        return  # cortex down / bad response → surface nothing

    # SCORE ALL CANDIDATES, THEN PICK (relevance decides; hardening only breaks ties). The
    # floor is applied INSIDE _best_note, to every bullet — not to one pre-chosen bullet.
    note, overlap = _best_note(query, resp.get("memory") or "")
    if not note:
        return  # nothing cleared the floor → stay quiet (no flood)

    # PER-SESSION DEDUPE — never surface the SAME note twice at the same (or weaker) relevance.
    fp = _fingerprint(note)
    if _already_surfaced(session_id, fp, overlap):
        return
    _record_surfaced(session_id, fp, overlap)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                "Cortex (my own operational memory) — relevant note before this "
                f"command: {note}"
            ),
        }
    }
    # NOTE: no permissionDecision → the tool proceeds under the normal flow; we
    # only attach context.
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never let a surfacing failure block the tool
    sys.exit(0)
