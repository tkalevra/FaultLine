#!/usr/bin/env python3
"""UserPromptSubmit hook — capture USER CORRECTIONS into the agent cortex.

The richest operational lessons are the moments the USER corrects the agent
(":8080 not :1234", "the field is `text` not `note`", "it's per-seat"). These
NEVER surface as a failed shell command, so cortex_capture.py can't see them.
This hook watches the submitted prompt for a curated set of correction signals
and, on a HIGH-PRECISION match, records a `category=correction` note (verbatim
correction + the prior assistant action it was correcting) via POST /iremember.

Design rails (match cortex_capture.py idiom):
  - HIGH PRECISION over recall (Christopher: "five things you corrected beats
    five hundred shell errors"). Curated, deterministic markers — NO LLM call.
    Better to MISS a correction than flood the cortex with noise.
  - DETERMINISTIC: regex markers only; no model, no fuzzy.
  - FAIL-SAFE: a hook error NEVER blocks or alters the user's prompt. We emit
    NOTHING to stdout (so the prompt is delivered untouched) and always exit 0.
  - CONTEXT-HONEST: the "what was being corrected" context is read from the
    transcript (prior assistant turn). If it can't be recovered cleanly we tag
    the note `needs-context` rather than fabricating what was corrected.

What this hook CAN see (verified against Claude Code 2.1.205):
  - stdin JSON carries: session_id, transcript_path, cwd, permission_mode,
    hook_event_name="UserPromptSubmit", and the submitted `prompt` text.
  - transcript_path is a JSONL of the whole conversation; the prior assistant
    turn (its `text` and last `tool_use`) is recoverable by scanning the tail.
Seat: env CORTEX_SEAT, else the shared dev-agent seat (same as cortex_capture).
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

# --- Curated correction markers (HIGH PRECISION) ---------------------------
# Each is a deterministic signal that the user is CORRECTING a prior action,
# not issuing a fresh task. Kept conservative on purpose.
_STRONG = [re.compile(p, re.I) for p in (
    r"^\s*no[,.\s!]",                                  # leading "No, ..."
    r"\bactually\b",
    r"\bthat'?s\s+(?:not|wrong|incorrect|backwards)\b",  # that's not / that's wrong
    r"\bthat\s+is\s+(?:not|wrong|incorrect)\b",
    r"\byou'?re\s+wrong\b",
    r"\b(?:incorrect|misread|misunderstood|mistake)\b",
    r"\bnot\s+\S+[,.]?\s+(?:but|it'?s|its|use)\b",      # "not X, but/use Y"
    r"\bit'?s\s+.+\bnot\b",                             # "it's X not Y"
    r"\byou\s+should(?:n'?t|'?ve|\s+have|\s+not)?\b",   # you should / shouldn't / should've
    r"\binstead\s+of\b",
    r"\bshould\s+be\b",
    r"\bi\s+(?:meant|said|told\s+you|asked\s+for)\b",
    r"\bdon'?t\b",
)]
# Aux-verb heads that make a bare "X not Y" NOT a correction ("is not", "do not"…).
_AUX = {"is", "are", "am", "was", "were", "be", "been", "being", "do", "does",
        "did", "can", "could", "will", "would", "should", "shall", "may",
        "might", "must", "has", "have", "had", "if", "or", "and", "why", "'s"}

# --- Injected / SYSTEM content guard (only genuine USER-TYPED prompts are corrections) ----
# UserPromptSubmit fires for EVERY submit, and the payload can carry harness-injected or
# system content (task notifications, system reminders, slash-command expansions). Those are
# NOT the user correcting the agent — capturing them stored a giant <task-notification> blob
# as a "USER CORRECTED me" note that then over-matched every recall. A genuine correction is
# short prose the user typed; injected content starts with (or is dominated by) a machine
# marker or a leading tag block. Match is deterministic and conservative.
_INJECTED_MARKERS = (
    "<task-notification",
    "<system-reminder",
    "<command-name",
    "<command-message",
    "[system notification",
    "important: after completing",
)
# A leading XML/HTML-ish tag (e.g. "<task-notification>", "<foo attr=...>") => injected block.
_LEADING_TAG_RE = re.compile(r"^\s*<\s*/?[a-zA-Z][\w:-]*(?:\s[^>]*)?/?>")

_CORRECTION_CAP = 300   # stored correction text hard cap (a blob can never over-match)


def _looks_injected(text):
    """True if `text` is harness-injected / system content rather than a genuine user
    correction. Deterministic: a leading machine marker, a leading tag block, or an
    injected marker near the very top."""
    raw = text.lstrip()
    low = raw.lower()
    if not low:
        return True
    for m in _INJECTED_MARKERS:
        if low.startswith(m):
            return True
    if _LEADING_TAG_RE.match(raw):
        return True
    # Defensive: a marker sitting in the first stretch (leading blank line / quote before it)
    # still means the prompt is dominated by injected content.
    head = low[:200]
    return any(m in head for m in _INJECTED_MARKERS)


def _detect(text):
    """Return a short marker label if `text` reads as a correction, else None."""
    low = text.strip().lower()
    if not low:
        return None
    for pat in _STRONG:
        if pat.search(low):
            return "signal"
    # Short punchy "A not B" value correction (":8080 not :1234", "text not note").
    # Only trust it when the clause is SHORT and the head isn't an aux verb — this
    # is the class of Christopher's examples and avoids incidental "does not exist".
    if len(low.split()) <= 12:
        m = re.search(r"(\S+)\s+not\s+(\S+)", low)
        if m and m.group(1) not in _AUX:
            return "value-swap"
    return None


def _read_prior_context(transcript_path):
    """Recover 'what was being corrected' from the transcript tail.

    Returns a tight string describing the prior assistant turn (its last text
    and/or last tool_use), or "" if nothing clean is recoverable.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", errors="replace") as fh:
            lines = fh.readlines()[-600:]  # bounded tail — fast on big transcripts
    except Exception:
        return ""
    last_text, last_tool = "", ""
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        content = ((obj.get("message") or {}).get("content")) or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and not last_text:
                last_text = (block.get("text") or "").strip()
            elif btype == "tool_use" and not last_tool:
                name = block.get("name") or "tool"
                inp = block.get("input") or {}
                arg = inp.get("command") or inp.get("file_path") or inp.get("query") or ""
                last_tool = f"{name}({str(arg)[:100]})" if arg else name
        if last_text or last_tool:
            break  # newest assistant turn found
    parts = []
    if last_tool:
        parts.append(f"ran {last_tool}")
    if last_text:
        parts.append(f'said "{last_text[:160]}"')
    return "; ".join(parts)


_STOP = {"the", "a", "an", "and", "or", "but", "not", "you", "your", "that",
         "this", "with", "for", "was", "are", "were", "its", "it's", "should",
         "have", "actually", "wrong", "instead", "dont", "don't", "meant",
         "said", "told", "asked", "incorrect", "there", "here", "then", "when"}


def _tags(text):
    """Salient recall triggers from the correction text (ports, IPs, backticked
    identifiers, and content words) — these are what irecall keys on later."""
    tags = set()
    for tok in re.findall(r"`([^`]+)`", text):          # `text`, `note`, ...
        tags.add(tok.strip().lower()[:32])
    tags.update(re.findall(r"\b\d{2,5}\b", text))        # ports / numbers
    tags.update(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))  # IPv4
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_./:-]{3,}", text):
        wl = w.lower()
        if wl not in _STOP:
            tags.add(wl[:32])
    tags.discard("")
    return sorted(tags)[:8]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return  # nothing to inspect
    if _looks_injected(prompt):
        return  # harness-injected / SYSTEM content → never a genuine correction; silent
    marker = _detect(prompt)
    if not marker:
        return  # not a correction → fast, silent no-op (prompt untouched)

    prior = _read_prior_context(data.get("transcript_path", ""))
    correction = prompt[:_CORRECTION_CAP]
    if prior:
        note = f'USER CORRECTED me — "{correction}" (I had just: {prior})'
    else:
        note = f'USER CORRECTED me — "{correction}"'
    tags = ["correction", "user-correction"] + _tags(prompt)
    if not prior:
        tags.append("needs-context")
    tags = sorted(set(tags))[:12]

    body = {"text": note, "category": "correction", "tags": tags}
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
        pass  # cortex down / offline → silently skip; never touch the prompt


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    # NO stdout on purpose: UserPromptSubmit stdout is injected as context. We stay
    # silent so the user's prompt is delivered untouched. Always succeed.
    sys.exit(0)
