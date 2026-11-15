"""Mechanical floor for the non-root runtime image (gauntlet dockerfile-nonroot-cache, bar DN5).

Parses the repo `Dockerfile` (no docker daemon needed) and pins the SHAPE that makes the
`USER faultline` runtime actually boot with its baked model caches:

  * the runtime user is created BEFORE the first COPY into the runtime stage (so no later layer
    is rewritten to fix ownership — every such rewrite is an overlayfs copy-up);
  * NO recursive chmod/chown anywhere, and no chmod/chown in the runtime stage after the cache COPY
    (the pre-fix `RUN chmod -R 755 /root/.cache /app` duplicated the 1.3 GB model layer);
  * ONE cache root (`FL_CACHE_ROOT`) declared as ARG+ENV in BOTH stages with the same default, and
    `HF_HOME`, `FASTEMBED_CACHE_PATH`, `NLTK_DATA` declared under it in BOTH stages (builder ENVs
    do not carry to runtime — the SPACY_MODEL lesson), BEFORE the builder bakes any model;
  * the cache COPY copies `${FL_CACHE_ROOT}` to itself (paths agree by construction);
  * no `$HOME`-keyed cache path (`/root/.cache`) survives anywhere;
  * `USER` names a non-root user, with an explicit UID/GID, and precedes `ENTRYPOINT`.

Each rule is also exercised against a MUTATED Dockerfile (the pre-fix shape re-introduced
programmatically) so the floor is proven to go red, not merely to pass.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "Dockerfile"

_MODEL_BAKE_RE = re.compile(r"from_pretrained|TextEmbedding\(|nltk\.downloader")


def parse_instructions(text: str) -> list[tuple[str, str]]:
    """Return [(INSTRUCTION, argument-text)] with comments dropped and `\\`-continuations joined."""
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        lines.append(buf.strip())
        buf = ""
    if buf:
        lines.append(buf.strip())
    out = []
    for ln in lines:
        m = re.match(r"([A-Za-z]+)\s+(.*)$", ln)
        if m:
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


def split_stages(instrs: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    stages: dict[str, list[tuple[str, str]]] = {}
    cur = None
    for ins, arg in instrs:
        if ins == "FROM":
            m = re.search(r"\bAS\s+(\S+)", arg, re.IGNORECASE)
            cur = m.group(1) if m else f"stage{len(stages)}"
            stages[cur] = []
        if cur is not None:
            stages[cur].append((ins, arg))
    return stages


def env_map(stage: list[tuple[str, str]]) -> dict[str, str]:
    """Collect ENV/ARG declarations (last wins) as {NAME: value} — `KEY=value` form only."""
    env: dict[str, str] = {}
    for ins, arg in stage:
        if ins in ("ENV", "ARG"):
            for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)=("([^"]*)"|\S+)', arg):
                env[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return env


def _first_index(stage, pred) -> int | None:
    for i, (ins, arg) in enumerate(stage):
        if pred(ins, arg):
            return i
    return None


def check(text: str) -> list[str]:
    """Return a list of violations (empty == the Dockerfile has the required shape)."""
    v: list[str] = []
    instrs = parse_instructions(text)
    stages = split_stages(instrs)
    if "builder" not in stages or "runtime" not in stages:
        return [f"expected stages 'builder' and 'runtime', got {sorted(stages)}"]
    builder, runtime = stages["builder"], stages["runtime"]

    # ── whole-file rules ──────────────────────────────────────────────────────────────────
    if any(re.search(r"/root/\.cache", arg) for _, arg in instrs):  # instructions only, not comments
        v.append("a $HOME-keyed cache path '/root/.cache' is still referenced by an instruction")
    for ins, arg in instrs:
        if ins == "RUN" and re.search(r"\b(chmod|chown)\s+(-[a-zA-Z]*R|--recursive)\b", arg):
            v.append(f"recursive chmod/chown in RUN: {arg[:80]}")

    # ── runtime stage: user before COPY, USER before ENTRYPOINT, non-root, explicit uid ───
    user_create = _first_index(runtime, lambda i, a: i == "RUN" and re.search(r"\b(useradd|adduser)\b", a))
    first_copy = _first_index(runtime, lambda i, a: i in ("COPY", "ADD"))
    if user_create is None:
        v.append("runtime stage never creates a user (useradd/adduser)")
    elif first_copy is not None and user_create > first_copy:
        v.append("runtime user is created AFTER the first COPY (ownership fixes would copy-up)")
    if user_create is not None and not re.search(r"useradd\b[^&|;]*\s-u\s", runtime[user_create][1]):
        v.append("runtime user is created without an explicit UID (useradd -u <uid>)")

    user_idx = _first_index(runtime, lambda i, a: i == "USER")
    entry_idx = _first_index(runtime, lambda i, a: i == "ENTRYPOINT")
    if user_idx is None:
        v.append("runtime stage has no USER instruction")
    else:
        if runtime[user_idx][1].strip() in ("root", "0", "0:0"):
            v.append("USER is root")
        if entry_idx is not None and user_idx > entry_idx:
            v.append("USER comes after ENTRYPOINT")
    if entry_idx is None:
        v.append("runtime stage has no ENTRYPOINT")

    # ── cache root: declared in BOTH stages, same default, loaders' envs rooted under it ───
    benv, renv = env_map(builder), env_map(runtime)
    for name, env in (("builder", benv), ("runtime", renv)):
        root = env.get("FL_CACHE_ROOT")
        if not root:
            v.append(f"{name} stage does not declare FL_CACHE_ROOT")
            continue
        for var in ("HF_HOME", "FASTEMBED_CACHE_PATH", "NLTK_DATA"):
            val = env.get(var)
            if val is None:
                v.append(f"{name} stage does not declare {var}")
            elif not (val.startswith("${FL_CACHE_ROOT}/") or val.startswith("$FL_CACHE_ROOT/")):
                v.append(f"{name} stage {var}={val!r} is not rooted under ${{FL_CACHE_ROOT}}")
    b_root = next((a for i, a in builder if i == "ARG" and a.startswith("FL_CACHE_ROOT=")), None)
    r_root = next((a for i, a in runtime if i == "ARG" and a.startswith("FL_CACHE_ROOT=")), None)
    if b_root is None or r_root is None:
        v.append("FL_CACHE_ROOT must be an ARG (with a default) in both stages")
    elif b_root != r_root:
        v.append(f"FL_CACHE_ROOT default differs between stages: {b_root!r} vs {r_root!r}")
    if "FL_CACHE_ROOT" in renv and not any(i == "ENV" and "FL_CACHE_ROOT=" in a for i, a in runtime):
        v.append("runtime stage must ENV FL_CACHE_ROOT (ARG alone is build-time only)")

    # builder: cache envs precede the first model bake
    bake = _first_index(builder, lambda i, a: i == "RUN" and _MODEL_BAKE_RE.search(a))
    for var in ("HF_HOME", "FASTEMBED_CACHE_PATH", "NLTK_DATA"):
        decl = _first_index(builder, lambda i, a, var=var: i == "ENV" and re.search(rf"\b{var}=", a))
        if bake is not None and decl is not None and decl > bake:
            v.append(f"builder declares {var} AFTER a model bake — the bake landed elsewhere")

    # runtime: the cache COPY moves ${FL_CACHE_ROOT} onto itself, and nothing chmods after it
    cache_copy = _first_index(
        runtime, lambda i, a: i == "COPY" and "--from=builder" in a and "FL_CACHE_ROOT" in a
    )
    if cache_copy is None:
        v.append("runtime stage has no `COPY --from=builder ${FL_CACHE_ROOT} ...`")
    else:
        parts = runtime[cache_copy][1].split()
        src, dst = parts[-2], parts[-1]
        if src != dst:
            v.append(f"cache COPY src/dst differ: {src} -> {dst}")
        for ins, arg in runtime[cache_copy + 1:]:
            if ins == "RUN" and re.search(r"\b(chmod|chown)\b", arg) and "docker-entrypoint.sh" not in arg:
                v.append(f"chmod/chown after the cache COPY (copy-up): {arg[:80]}")
    # runtime: envs are declared BEFORE the cache COPY so the destination and the env agree
    if cache_copy is not None:
        for var in ("HF_HOME", "FASTEMBED_CACHE_PATH", "NLTK_DATA"):
            decl = _first_index(runtime, lambda i, a, var=var: i == "ENV" and re.search(rf"\b{var}=", a))
            if decl is None or decl > cache_copy:
                v.append(f"runtime declares {var} after (or never before) the cache COPY")
    return v


# ── the floor ────────────────────────────────────────────────────────────────────────────
def test_dockerfile_has_nonroot_cache_shape():
    text = DOCKERFILE.read_text()
    assert check(text) == []


# ── the floor goes RED on each pre-fix shape (mutation self-check) ───────────────────────
def _mutate(text: str, old: str, new: str) -> str:
    assert old in text, f"mutation anchor missing: {old!r}"
    return text.replace(old, new, 1)


@pytest.mark.parametrize(
    "label,mutate,expect",
    [
        (
            "chmod -R after cache COPY (the pre-fix copy-up)",
            lambda t: _mutate(t, "\nUSER faultline", "\nRUN chmod -R 755 /app\nUSER faultline"),
            "recursive chmod",
        ),
        (
            "user created after the COPYs",
            lambda t: re.sub(r"RUN groupadd[^\n]*\\\n[^\n]*useradd[^\n]*\n", "", t, count=1)
            + "",  # then re-add it right before USER, i.e. after every COPY
            "created AFTER the first COPY",
        ),
        (
            "HF_HOME missing from runtime stage",
            lambda t: t.replace("ENV HF_HOME=${FL_CACHE_ROOT}/huggingface\n", "", 1)
            if t.count("ENV HF_HOME=") >= 2 else t,
            "does not declare HF_HOME",
        ),
        (
            "$HOME-keyed cache path re-introduced",
            lambda t: _mutate(t, "ENV FASTEMBED_CACHE_PATH=${FL_CACHE_ROOT}/fastembed\n", "ENV FASTEMBED_CACHE_PATH=/root/.cache/fastembed\n"),
            "/root/.cache",
        ),
        (
            "USER after ENTRYPOINT",
            lambda t: _mutate(t, "USER faultline\n\nENTRYPOINT [\"./docker-entrypoint.sh\"]", "ENTRYPOINT [\"./docker-entrypoint.sh\"]\nUSER faultline"),
            "USER comes after ENTRYPOINT",
        ),
        (
            "stages disagree on the cache root",
            lambda t: _mutate(t, "ARG FL_CACHE_ROOT=/opt/faultline/cache\nENV FL_CACHE_ROOT=${FL_CACHE_ROOT}\n# huggingface_hub",
                              "ARG FL_CACHE_ROOT=/var/cache/faultline\nENV FL_CACHE_ROOT=${FL_CACHE_ROOT}\n# huggingface_hub"),
            "default differs between stages",
        ),
    ],
)
def test_floor_goes_red_on_prefix_shape(label, mutate, expect):
    text = DOCKERFILE.read_text()
    mutated = mutate(text)
    if label.startswith("user created after"):
        # move the user creation to just before USER (after every COPY)
        mutated = mutated.replace(
            "\nUSER faultline",
            "\nRUN groupadd -r -g 10001 faultline && useradd --no-log-init -r -u 10001 -g faultline faultline\nUSER faultline",
            1,
        )
    assert mutated != text, f"mutation did not apply: {label}"
    violations = check(mutated)
    assert any(expect in x for x in violations), f"{label}: expected a violation containing {expect!r}, got {violations}"


# ── .dockerignore must exclude NESTED bytecode under Docker's ROOTED matching ─────────────────
#
# Docker anchors every .dockerignore pattern to the context root (moby/patternmatcher, Go
# filepath.Match per segment; only `**` spans directories). A bare `__pycache__/` therefore
# matches ./__pycache__ ONLY, and src/api/__pycache__/x.pyc rides into the image. That is
# exactly what shipped until 2026-09-15, when the runtime-stage readability gate above refused
# a root-owned 0600 .pyc left on the pre-prod host by a sudo run. This pin does not grep for
# a spelling — it emulates the rooted matcher and asks whether the nested path is excluded,
# so a regression to any form Docker would not honour goes red.
DOCKERIGNORE = REPO / ".dockerignore"


def _docker_ignore_patterns() -> list[str]:
    out = []
    for raw in DOCKERIGNORE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.rstrip("/"))  # moby strips a trailing separator
    return out


def _segments_match(pat: list[str], path: list[str]) -> bool:
    """Rooted, segment-wise fnmatch with `**` spanning zero or more directories."""
    import fnmatch
    if not pat:
        return not path
    if pat[0] == "**":
        return any(_segments_match(pat[1:], path[i:]) for i in range(len(path) + 1))
    if not path or not fnmatch.fnmatchcase(path[0], pat[0]):
        return False
    return _segments_match(pat[1:], path[1:])


def _docker_excludes(rel_path: str) -> bool:
    """True iff Docker would drop rel_path from the build context.

    A file is excluded when the pattern matches the path OR any of its parent directories
    (moby's parent-dir rule); a leading `!` re-includes (last match wins), as in Docker.
    """
    parts = rel_path.split("/")
    excluded = False
    for p in _docker_ignore_patterns():
        negate = p.startswith("!")
        pat = p[1:].split("/") if negate else p.split("/")
        hit = any(_segments_match(pat, parts[:n]) for n in range(1, len(parts) + 1))
        if hit:
            excluded = not negate
    return excluded


@pytest.mark.parametrize("nested_artifact", [
    "src/api/__pycache__/hardline_guard.cpython-311.pyc",   # the one the gate refused
    "saas/__pycache__/acl.cpython-311.pyc",
    "src/mcp/deep/er/__pycache__/x.pyc",
    "tools/gauntlet.pyc",                                    # stray bytecode outside a cache dir
])
def test_dockerignore_excludes_nested_bytecode(nested_artifact):
    assert _docker_excludes(nested_artifact), (
        f"{nested_artifact} would enter the build context — .dockerignore patterns are rooted; "
        "nested bytecode needs the `**/` form")


@pytest.mark.parametrize("source", [
    "src/api/hardline_guard.py", "saas/acl.py", "Dockerfile", "migrations/274_episodic_log_turn_key.sql",
])
def test_dockerignore_keeps_sources(source):
    assert not _docker_excludes(source), f"{source} must stay in the build context"


def test_rooted_matcher_reproduces_the_defect():
    """Anti-vacuity: the emulator itself must show a bare `__pycache__` pattern does NOT reach
    a nested path — otherwise the pin above could pass on a matcher that ignores rooting."""
    assert _segments_match(["__pycache__"], ["__pycache__"])
    assert not any(_segments_match(["__pycache__"], ["src", "api", "__pycache__"][:n]) for n in (1, 2, 3))
    assert _segments_match(["**", "__pycache__"], ["src", "api", "__pycache__"])
