# FOSS-003 — setup.sh dies with "env: can't execute 'bash'" on bash-less Docker hosts, before its own python3 check ever runs

- **Branch matrix:** es 761cf914 reproduced · it 9f2724ac same file (byte-identical) · main c0fe9322 same file
- **Severity:** Low-Medium (portability; the documented fallback exists but the primary documented command fails confusingly)
- **Discovered:** 2026-10-17, fresh-install gauntlet on an Alpine docker:dind host

## Symptom

On a minimal Docker host (Alpine-based appliance/containers, some NAS distros — the exact "self-host on the box you already have" audience the README courts), the README's primary command fails:

    $ ./setup.sh
    env: can't execute 'bash': No such file or directory

setup.sh's FIRST act is checking for python3 and printing a helpful install message — but that code is unreachable because the file's shebang (`#!/usr/bin/env bash`) aborts the interpreter lookup first. The README's Requirements section lists Docker/Compose, an LLM backend and RAM — **bash is never mentioned**.

## Root cause

setup.sh is a two-line delegator to quickstart.py (stdlib-only, 3.8+). Its only bash-ism is... nothing: the script body is plain POSIX sh. The bash shebang is pure accident of authoring.

## Reproduce

1. Any POSIX host without bash (docker:dind used in the gauntlet).
2. git clone + ./setup.sh -> env: can't execute 'bash'.
3. python3 quickstart.py works fine on the same host (after installing python3, which setup.sh would have helped with).

## Fix direction (gauntlet work order)

1. Change the shebang to `#!/bin/sh` (the body is already POSIX) — OR keep bash but add a sh-safe guard: re-exec via `command -v bash` check with a clear message that ALSO mentions `python3 quickstart.py` as the fallback.
2. Add "bash (only for ./setup.sh; or run python3 quickstart.py directly)" to README Requirements — whichever way the fix lands, the docs must name the real dependency.
3. Pin: a shellcheck (or sh -n) test over setup.sh asserting POSIX compatibility, and a doc test asserting the README Requirements section mentions the launcher's interpreter requirement.
4. **You are NOT your own critic.** When green, request a clean critic agent (semgrep + codeql + POSIX-sh review) before merge.
