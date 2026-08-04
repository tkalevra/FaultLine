#!/usr/bin/env python3
"""Stop hook — the THIRD cortex lane: lessons I CONCLUDE.

The other two lanes are already automatic:
  * cortex_capture.py   (PostToolUse)      -> a command FAILED   -> auto `failed_command` note
  * cortex_correction.py(UserPromptSubmit) -> the USER CORRECTED me -> auto `correction` note
  * cortex_surface.py   (PreToolUse)       -> surfaces relevant notes before a command

Neither fires for the lane the owner keeps having to PROMPT for: a lesson I worked out by
READING — no command failed, nobody corrected me. e.g. "a `query_detected` turn still harvests
buried facts, so never infer stored/dropped from status — use the DB delta." That insight is
the most valuable kind and the easiest to lose, because nothing in the machinery marks it.

So: at the end of a session that did REAL WORK (source edits / commits / test or bench runs),
if I recorded NO deliberate note (gotcha | pitfall | howto | correction — `failed_command` does
NOT count, it is auto-captured), block the stop ONCE and make me write one.

Fail-safe by construction: any error, any doubt -> allow the stop. A hook that wedges the agent
is worse than a missed note. It also never blocks twice (`stop_hook_active`).
"""
import json
import sys

# A note in one of these categories is a DELIBERATE lesson. `failed_command` is excluded on
# purpose: it is auto-captured by the PostToolUse hook, so counting it would let the gate be
# satisfied by machinery rather than by thought.
DELIBERATE = {"gotcha", "pitfall", "howto", "correction", "overrun"}

# Signals that this session actually DID something worth a lesson.
WORK_TOOLS = {"Edit", "Write", "NotebookEdit"}
WORK_CMD_HINTS = ("git commit", "fltest.py", "dig_lme.py", "run_claims.py", "run_longmemeval.py",
                  "docker build", "pytest")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # can't read -> allow

    # Never block twice: if we already blocked once this session, let it go.
    if payload.get("stop_hook_active"):
        return

    path = payload.get("transcript_path")
    if not path:
        return

    did_work = False
    wrote_lesson = False

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                msg = ev.get("message") or {}
                for block in (msg.get("content") or []):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or ""
                    inp = block.get("input") or {}

                    # Did I record a DELIBERATE lesson?
                    if "iremember" in name:
                        cat = (inp.get("category") or "gotcha").lower()
                        if cat in DELIBERATE:
                            wrote_lesson = True

                    # Did I do real work?
                    if name in WORK_TOOLS:
                        did_work = True
                    elif name == "Bash":
                        cmd = (inp.get("command") or "").lower()
                        if any(h in cmd for h in WORK_CMD_HINTS):
                            did_work = True
    except Exception:
        return  # unreadable transcript -> allow

    if did_work and not wrote_lesson:
        print(json.dumps({
            "decision": "block",
            "reason": (
                "CORTEX LESSON GATE — this session changed code / ran tests / committed, but you "
                "recorded NO deliberate operational note.\n\n"
                "The auto-lanes only catch failed commands and the user's corrections. The lane "
                "that keeps getting lost is the one you WORK OUT BY READING — nothing failed, "
                "nobody corrected you, and the insight evaporates.\n\n"
                "Before finishing, ask: what would I want to be told before I next touch this?\n"
                "  - a gotcha (a trap in the tooling/infra you just hit)\n"
                "  - a pitfall (a wrong assumption you nearly shipped)\n"
                "  - a howto (a procedure you had to work out)\n"
                "Record it with `iremember` (category gotcha|pitfall|howto), with the EVIDENCE. "
                "If you genuinely learned nothing worth keeping, say so in one line and stop — "
                "this gate fires only once per session."
            ),
        }))
        sys.exit(0)


if __name__ == "__main__":
    main()
