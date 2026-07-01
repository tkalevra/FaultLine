#!/usr/bin/env python3
"""
FaultLine quickstart — interactive setup wizard.

Cross-platform (Linux / macOS / Windows). Stdlib only — no `pip install`, just Python 3.8+.

  python3 quickstart.py            # full interactive setup (writes .env)
  python3 quickstart.py --validate # re-run the LLM connectivity check only
  python3 quickstart.py --help     # this help

What it does:
  1. Checks your platform + that Docker / Docker Compose are installed.
  2. Asks for your LLM backend + connection details, then CONNECTS and lists the
     models it actually finds so you can pick one (no blind typing). If a
     connection fails you can go back and fix the URL/key.
  3. Offers to generate a secret MCP_API_KEY.
  4. Resolves your tenant id (FAULTLINE_USER_ID): generate, reuse, enter
     manually, or leave blank for OpenWebUI multi-user.
  5. Writes a ready-to-use .env (keeping the correct Docker Compose Postgres host).
  6. Prints the exact next steps.

FaultLine hooks into an LLM you already run; it does not host one.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import socket
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_EXAMPLE = os.path.join(HERE, ".env.example")
ENV_PATH = os.path.join(HERE, ".env")

# ── tiny cross-platform color (auto-off on dumb terminals / Windows w/o ANSI) ──
_USE_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and (platform.system() != "Windows" or os.environ.get("WT_SESSION") or os.environ.get("ANSICON"))
)


def _c(code, s):  return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s
def bold(s):   return _c("1", s)
def green(s):  return _c("32", s)
def yellow(s): return _c("33", s)
def red(s):    return _c("31", s)
def cyan(s):   return _c("36", s)
def dim(s):    return _c("2", s)


def hr():
    print(dim("─" * 68))


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{cyan('?')} {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)
    return val or default


def ask_yes(prompt, default_yes=True):
    d = "Y/n" if default_yes else "y/N"
    val = ask(f"{prompt} ({d})").lower()
    return default_yes if not val else val in ("y", "yes")


def choose(prompt, options):
    """options = [(key, label)]; returns key."""
    print(f"\n{bold(prompt)}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {bold(str(i))}. {label}")
    while True:
        raw = ask("Choose", "1")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(red("  Please enter a number from the list."))


# ── platform + prerequisites ──────────────────────────────────────────────────
def check_prereqs():
    sysname = platform.system()
    pretty = {"Linux": "Linux", "Darwin": "macOS", "Windows": "Windows"}.get(sysname, sysname)
    print(f"Platform detected: {bold(pretty)}  (Python {platform.python_version()})")
    if not shutil.which("docker"):
        print(red("  ✗ Docker not found on PATH.  Install: ") + cyan("https://docs.docker.com/get-docker/"))
        return
    print(green("  ✓ docker found"))
    try:
        out = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=15)
        print(green("  ✓ docker compose (v2) available") if out.returncode == 0
              else yellow("  ! `docker compose` not available — you need Compose v2+."))
    except Exception:
        print(yellow("  ! could not run `docker compose version` (is the Docker daemon running?)"))


# ── HTTP helper ───────────────────────────────────────────────────────────────
def _http_get(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _probe_url(url):
    """host-side probe target: undo the container rewrite so we test from here."""
    return url.replace("host.docker.internal", "localhost").rstrip("/")


# ── connect + list models (the "query for available options" step) ────────────
def probe_models(backend, base_url, key):
    """
    Connect to the backend and list models. Returns (ok, [model_ids], detail).
    ok=False means unreachable / auth failed — caller offers a way back.
    """
    base = _probe_url(base_url)
    try:
        if backend in ("lm_studio", "openai", "openwebui"):
            path = "/api/models" if backend == "openwebui" else "/v1/models"
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            status, body = _http_get(base + path, headers=headers)
            data = []
            try:
                p = json.loads(body)
                data = p.get("data") or p.get("models") or []
            except Exception:
                pass
            ids = [m.get("id") or m.get("name") for m in data if isinstance(m, dict)]
            return True, [i for i in ids if i], f"HTTP {status}, {len(ids)} model(s)"

        if backend == "ollama":
            status, body = _http_get(base + "/api/tags")
            names = []
            try:
                names = [m.get("name") for m in (json.loads(body).get("models") or [])]
            except Exception:
                pass
            return True, [n for n in names if n], f"HTTP {status}, {len(names)} pulled"

        if backend == "anthropic":
            if not key:
                return False, [], "no API key entered"
            status, body = _http_get(base + "/v1/models",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            ids = []
            try:
                ids = [m.get("id") for m in (json.loads(body).get("data") or [])]
            except Exception:
                pass
            return True, [i for i in ids if i], f"HTTP {status}, {len(ids)} model(s)"

    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, [], f"HTTP {e.code} — auth failed (check your API key)"
        return False, [], f"HTTP {e.code} — endpoint error"
    except Exception as e:
        return False, [], f"{type(e).__name__}: {e}"
    return False, [], "unsupported backend"


def pick_model(models, default=""):
    """Let the user pick from the discovered list, or type a custom id."""
    if not models:
        return ask("Model id", default)
    print(f"\n  {bold('Models available on your server:')}")
    show = models[:20]
    for i, m in enumerate(show, 1):
        print(f"    {bold(str(i))}. {m}")
    print(dim("    (or type a different id)"))
    raw = ask("Pick a number or type a model id", "1")
    if raw.isdigit() and 1 <= int(raw) <= len(show):
        return show[int(raw) - 1]
    return raw


# ── LLM backend configuration (connect → query → pick, with back-navigation) ──
_BACKENDS = [
    ("lm_studio", "LM Studio        (local desktop app, OpenAI-compatible, no auth)"),
    ("ollama",    "Ollama           (local, `ollama serve`)"),
    ("openwebui", "OpenWebUI        (self-hosted UI in front of a model)"),
    ("openai",    "OpenAI-compatible (any /v1 endpoint — vLLM, LiteLLM, OpenAI, etc.)"),
    ("anthropic", "Anthropic        (Claude API)"),
]


def _prompt_connection(backend):
    """Gather URL (+ key) with per-backend guidance. Returns (url, key)."""
    if backend == "lm_studio":
        print(dim("\n  LM Studio: open the Developer tab (server icon) and toggle the server On"))
        print(dim("  (or run `lms server start`). LOAD a model first. Default port 1234. No API key"))
        print(dim("  needed (LM Studio ignores it on localhost). URL is host:port only (/v1 appended)."))
        return ask("LM Studio server URL", "http://localhost:1234"), ""

    if backend == "ollama":
        print(dim("\n  Ollama: run `ollama serve`, then `ollama pull <model>` (e.g. qwen2.5). Port 11434."))
        return ask("Ollama URL", "http://localhost:11434"), ""

    if backend == "openwebui":
        print(dim("\n  OpenWebUI needs its base URL + an API key:"))
        print(dim("  1. Enable keys once (admin): Admin Panel → Settings → General → 'Enable API Keys'"))
        print(dim("     (until enabled, the section below is hidden; on v0.6.40+ non-admins may also"))
        print(dim("     need the API Keys feature permission for their group)."))
        print(dim("  2. Your key: Profile (bottom-left) → Settings → Account → API Keys →"))
        print(dim("     'Generate New API Key'. Copy it now — it's shown only once."))
        print(dim("  Base URL = host:port only (container port is usually 8080; host-published often 3000)."))
        return ask("OpenWebUI base URL", "http://open-webui:8080"), ask("OpenWebUI API key (sk-...)", "")

    if backend == "openai":
        print(dim("\n  Any OpenAI-compatible /v1 endpoint (OpenAI, vLLM, LiteLLM, Together, ...)."))
        print(dim("  URL = host root, no /v1 path (it's appended)."))
        return ask("Base URL", "https://api.openai.com"), ask("API key", "")

    if backend == "anthropic":
        print(dim("\n  Anthropic Claude API. Get a key at https://console.anthropic.com/."))
        return ask("Base URL", "https://api.anthropic.com"), ask("Anthropic API key (sk-ant-...)", "")

    return ask("Base URL", ""), ask("API key", "")


_DEFAULT_MODEL = {
    "lm_studio": "qwen/qwen3.5-9b", "ollama": "qwen2.5", "openwebui": "qwen/qwen3.5-9b",
    "openai": "gpt-4o-mini", "anthropic": "claude-3-5-sonnet-latest",
}


def configure_backend():
    backend = choose(
        "Which LLM are you already running? (FaultLine connects to it — it doesn't host one)",
        _BACKENDS,
    )
    cfg = {"LLM_BACKEND_TYPE": backend, "LLM_API_KEY": "", "WGM_LLM_MODEL": "", "LLM_BASE_URL": ""}

    while True:
        url, key = _prompt_connection(backend)
        print(f"\n{bold('Connecting')} → {_probe_url(url)} ...")
        ok, models, detail = probe_models(backend, url, key)

        if ok:
            print(green(f"  ✓ connected ({detail})"))
            cfg["LLM_BASE_URL"], cfg["LLM_API_KEY"] = url, key
            cfg["WGM_LLM_MODEL"] = pick_model(models, _DEFAULT_MODEL.get(backend, ""))
            if models and cfg["WGM_LLM_MODEL"] not in models:
                print(yellow(f"  ! '{cfg['WGM_LLM_MODEL']}' isn't in the list — make sure it's loaded/pulled."))
            return cfg

        # connection failed — let the user fix it or bail (never dead-end)
        print(red(f"  ✗ could not connect — {detail}"))
        print(dim("    Is the LLM running? Is the host/port/key right? (local apps: start the server)"))
        action = choose("What now?", [
            ("retry",  "Re-enter the connection details (URL / key) and try again"),
            ("backend", "Pick a different LLM backend"),
            ("skip",   "Continue anyway — I'll type the model id and fix connectivity later"),
            ("quit",   "Quit"),
        ])
        if action == "retry":
            continue
        if action == "backend":
            backend = choose("Which LLM backend?", _BACKENDS)
            cfg["LLM_BACKEND_TYPE"] = backend
            continue
        if action == "skip":
            cfg["LLM_BASE_URL"], cfg["LLM_API_KEY"] = url, key
            cfg["WGM_LLM_MODEL"] = ask("Model id", _DEFAULT_MODEL.get(backend, ""))
            return cfg
        print("Aborted.")
        sys.exit(1)


# ── tenant identity (FAULTLINE_USER_ID) ───────────────────────────────────────
def configure_identity():
    """
    Resolve FAULTLINE_USER_ID (the per-tenant memory owner).
    Returns a UUID for single-user/direct use, or "" for OpenWebUI multi-user
    (where OWUI injects a real per-user id via the header and pinning one would
    collapse every user into a single shared memory).

    Note: OpenWebUI exposes no documented API to fetch the current user's id from
    an API key, so there is no reliable programmatic autodetect — you copy it from
    the UI (Admin Panel → Users) when you need a specific one.
    """
    existing = ""
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            if line.strip().startswith("FAULTLINE_USER_ID="):
                existing = line.split("=", 1)[1].strip()

    opts = [
        ("generate", "Generate a new private ID   (recommended for solo / direct use)"),
        ("manual",   "Enter it manually           (e.g. paste your OpenWebUI user id)"),
        ("blank",    "Leave blank — multi-user via OpenWebUI (each user auto-scoped by header)"),
    ]
    if existing:
        opts.insert(0, ("reuse", f"Keep the id already in .env  ({existing})"))

    while True:
        mode = choose("Set your FAULTLINE_USER_ID (the memory tenant owner):", opts)

        if mode == "reuse":
            return existing
        if mode == "generate":
            uid = str(uuid.uuid4())
            print(green(f"  ✓ generated your private tenant id: {uid}"))
            print(dim("    (also send it as the X-OpenWebUI-User-Id header on direct MCP calls)"))
            return uid
        if mode == "manual":
            print(dim("  Find your OpenWebUI user id in the UI: Admin Panel → Users (it's a UUID)."))
            uid = ask("FAULTLINE_USER_ID (UUID)", existing)
            if uid:
                return uid
            print(yellow("  ! empty — pick again."))
            continue
        if mode == "blank":
            print(dim("\n  OpenWebUI supplies each user's ID automatically — don't pin one"))
            print(dim("  (pinning would put every user in the SAME memory). Set up two things:"))
            print(dim("   1. On OpenWebUI, set  " + bold("ENABLE_FORWARD_USER_INFO_HEADERS=true")))
            print(dim("      in its environment — that's what makes it send the X-OpenWebUI-User-Id"))
            print(dim("      header. Without it, per-user memory won't work. (This forwards over the"))
            print(dim("      OpenAPI tool-server connection, not native MCP — see open-webui#21134.)"))
            print(dim("   2. Find a user's id (for testing): Admin Panel → Users."))
            return ""


# ── .env writing ──────────────────────────────────────────────────────────────
def _host_for_container(url):
    """localhost → host.docker.internal so the container can reach a host-local LLM."""
    for local in ("localhost", "127.0.0.1"):
        if f"//{local}:" in url or url.endswith(f"//{local}"):
            return url.replace(local, "host.docker.internal")
    return url


def write_env(cfg, mcp_key, faultline_user_id=""):
    if not os.path.exists(ENV_EXAMPLE):
        print(red(f"  ✗ {ENV_EXAMPLE} not found — run this from the FaultLine repo root."))
        sys.exit(1)
    if os.path.exists(ENV_PATH):
        if not ask_yes(".env already exists — overwrite? (a backup is made)", default_yes=False):
            print(yellow("  Kept existing .env. Nothing written."))
            return
        shutil.copy(ENV_PATH, ENV_PATH + ".bak")
        print(dim("  backed up existing .env → .env.bak"))

    to_set = {
        "LLM_BACKEND_TYPE": cfg["LLM_BACKEND_TYPE"],
        "LLM_BASE_URL": _host_for_container(cfg["LLM_BASE_URL"]),
        "LLM_API_KEY": cfg.get("LLM_API_KEY", ""),
        "WGM_LLM_MODEL": cfg["WGM_LLM_MODEL"],
        "FAULTLINE_USER_ID": faultline_user_id,
    }
    if mcp_key:
        to_set["MCP_API_KEY"] = mcp_key

    lines = open(ENV_EXAMPLE, encoding="utf-8").readlines()
    seen, out = set(), []
    for line in lines:
        s = line.lstrip()
        matched = next((k for k in to_set
                        if s.startswith(f"{k}=") or s.startswith(f"# {k}=") or s.startswith(f"#{k}=")), None)
        if matched and matched not in seen:
            out.append(f"{matched}={to_set[matched]}\n")
            seen.add(matched)
        elif matched:
            continue  # drop duplicate declarations
        else:
            out.append(line)
    for k, v in to_set.items():
        if k not in seen:
            out.append(f"{k}={v}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(out)
    print(green(f"  ✓ wrote {ENV_PATH}"))


# ── next steps ────────────────────────────────────────────────────────────────
def print_next_steps(mcp_key):
    hr()
    print(bold("You're set. Next steps:\n"))
    print(f"  1. Build + start the stack:   {cyan('docker compose up -d --build')}")
    print(f"  2. Wait ~60s, then check:     {cyan('curl http://localhost:8000/health')}")
    print(f"       expect: {dim(chr(34) + 'database:ok, qdrant:ok, llm:ok' + chr(34))}")
    print(f"  3. The live integration path is the MCP server on {bold(':8002')}.")
    if mcp_key:
        print(f"       Secured with your MCP_API_KEY — send {cyan('Authorization: Bearer <key>')}.")
    print()
    print(dim("  Troubleshooting:"))
    print(dim("    docker compose logs faultline          # backend startup / errors"))
    print(dim("    docker compose exec postgres psql -U faultline -d faultline -c 'SELECT 1'"))
    print(dim("    python3 quickstart.py --validate       # re-test LLM connectivity"))
    print(dim("    See DEPLOYMENT.md and docs/ENV-REFERENCE.md for the full reference."))
    hr()


def _poll_health(timeout=80):
    """Poll the backend /health after a build so the user sees it actually came up."""
    print(dim("  waiting for the backend to come up (~60s on first boot)..."))
    for _ in range(max(1, timeout // 5)):
        try:
            _, body = _http_get("http://localhost:8000/health", timeout=4)
            d = json.loads(body)
            print(green(f"  ✓ backend up — database:{d.get('database')} "
                        f"qdrant:{d.get('qdrant')} llm:{d.get('llm')}"))
            if d.get("llm") != "ok":
                print(yellow("    (llm not ok — check LLM_BASE_URL and that your model is loaded/pulled)"))
            return
        except Exception:
            time.sleep(5)
    print(yellow("  ! not healthy yet within the wait — check: docker compose logs faultline"))


# ── client integration guide (copy THIS into THAT, with real values) ──────────
def _detect_lan_ip():
    """Best-effort primary LAN IP (no traffic actually sent). '' if undetectable."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def _claude_config_path():
    sysname = platform.system()
    if sysname == "Darwin":
        return "~/Library/Application Support/Claude/claude_desktop_config.json"
    if sysname == "Windows":
        return r"%APPDATA%\Claude\claude_desktop_config.json"
    return "~/.config/Claude/claude_desktop_config.json"


def print_integration_guide(mcp_key, user_id):
    """Tailored 'paste this value there' wrap-up for the user's chosen client."""
    key = mcp_key or "<your MCP_API_KEY>"
    uid = user_id or "<your user UUID>"
    lan = _detect_lan_ip()
    host_note = f"localhost (same machine as Docker){f' or {lan} (from another device)' if lan else ''}"
    host = lan or "localhost"

    print(bold("\nConnect a client to FaultLine's memory (MCP server on :8002):"))
    client = choose("Which client will use FaultLine?", [
        ("claude", "Claude Desktop"),
        ("owui",   "OpenWebUI"),
        ("mcp",    "Cursor / other MCP client"),
        ("curl",   "Direct API (curl / your own scripts)"),
        ("all",    "Show all / not sure"),
    ])

    def claude_block():
        print(bold("\n  Claude Desktop"))
        print(dim(f"  Edit your config file:  {_claude_config_path()}   (create it if missing)"))
        print(dim("  Add this (host = " + host_note + "):"))
        print(cyan(f"""  {{
    "mcpServers": {{
      "faultline": {{
        "url": "http://{host}:8002/mcp",
        "headers": {{ "Authorization": "Bearer {key}" }}
      }}
    }}
  }}"""))
        print(dim("  Restart Claude Desktop → the 🔨 tools icon should appear (needs a recent version)."))
        print(dim("  If your version can't send headers to a remote MCP, use the mcp-remote bridge instead:"))
        print(dim(f'    "command": "npx", "args": ["mcp-remote", "http://{host}:8002/mcp", "--header", "Authorization: Bearer {key}"]'))

    def owui_block():
        print(bold("\n  OpenWebUI") + dim("   (v0.10.x paths; from v0.6.31+)"))
        print(dim("  Recommended — OpenAPI tool server:  Settings → Tools → +   (instance-wide: Admin Settings → Tools)"))
        print(dim("   • URL:  ") + cyan(f"http://faultline-mcp:8002") +
              dim("   (if OWUI is in the same compose network; else ") + cyan(f"http://{host}:8002") + dim(")"))
        print(dim("   • Auth: Bearer  →  ") + cyan(key))
        print(dim("   • The modal may pre-fill https:// — change it to http:// for a local server, then hit refresh."))
        print(dim("  OWUI reads /openapi.json and exposes recall_memory / remember_facts / retract_fact."))
        print(dim("  Also set  ") + bold("ENABLE_FORWARD_USER_INFO_HEADERS=true") + dim("  on OWUI so per-user memory works"))
        print(dim("  (forwards the X-OpenWebUI-User-Id header FaultLine scopes on)."))
        print(dim("  Native MCP (Admin Settings → External Tools → + → MCP (Streamable HTTP), ") + cyan(f"http://{host}:8002/mcp") + dim(")"))
        print(dim("  also works, but does NOT forward the user-id header yet (open-webui#21134) —"))
        print(dim("  use the OpenAPI path above for per-user memory, or pin FAULTLINE_USER_ID for single-user."))
        print(dim("  Weak model not firing tools? Chat Controls → Advanced Params → Function Calling → Legacy"))
        print(dim("  (v0.10.0 defaults to Native; 'Default' was renamed 'Legacy')."))

    def mcp_block():
        print(bold("\n  Cursor / other MCP client"))
        print(dim("  Add an HTTP (streamable-http) MCP server:"))
        print(dim("   • URL:    ") + cyan(f"http://{host}:8002/mcp"))
        print(dim("   • Header: ") + cyan(f"Authorization: Bearer {key}"))

    def curl_block():
        print(bold("\n  Direct API (curl / scripts)"))
        print(cyan(f"""  curl -X POST http://{host}:8002/recall_memory \\
    -H "Authorization: Bearer {key}" \\
    -H "X-OpenWebUI-User-Id: {uid}" \\
    -H "Content-Type: application/json" \\
    -d '{{"query": "what do you know about me?"}}'"""))

    blocks = {"claude": claude_block, "owui": owui_block, "mcp": mcp_block, "curl": curl_block}
    if client == "all":
        for b in (claude_block, owui_block, mcp_block, curl_block):
            b()
    else:
        blocks[client]()

    print(dim("\n  Note: the FIRST call provisions your private memory (~1 min). An empty first"))
    print(dim("  recall is normal, not an error — the tenant is being created."))
    print(dim("  Tip: tell your model (system prompt) to call recall_memory before answering and"))
    print(dim("  remember_facts on new facts — otherwise it may not use the memory. See DEPLOYMENT.md."))
    hr()


def _load_env(path):
    cfg = {"LLM_BACKEND_TYPE": "", "LLM_BASE_URL": "", "LLM_API_KEY": "", "WGM_LLM_MODEL": ""}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k in cfg:
            cfg[k] = v.strip()
    return cfg


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()
    if args.help:
        print(__doc__)
        return

    if args.validate:
        path = ENV_PATH if os.path.exists(ENV_PATH) else ENV_EXAMPLE
        cfg = _load_env(path)
        if not cfg["LLM_BACKEND_TYPE"]:
            print(red("No LLM backend configured — run `python3 quickstart.py` first."))
            sys.exit(1)
        print(f"{bold('Connectivity check')} → {_probe_url(cfg['LLM_BASE_URL'])}")
        ok, models, detail = probe_models(cfg["LLM_BACKEND_TYPE"], cfg["LLM_BASE_URL"], cfg["LLM_API_KEY"])
        if ok:
            print(green(f"  ✓ connected ({detail})"))
            m = cfg["WGM_LLM_MODEL"]
            if m and models:
                print(green(f"  ✓ model '{m}' available") if m in models
                      else yellow(f"  ! model '{m}' not in the list — load/pull it"))
        else:
            print(red(f"  ✗ {detail}"))
        sys.exit(0 if ok else 2)

    print(bold(cyan("\n  FaultLine — quickstart setup\n")))
    print("  A per-tenant, write-validated knowledge-graph memory for your LLM.")
    print(dim("  Connects to your LLM, confirms it, and writes a ready-to-use .env.\n"))
    hr()
    check_prereqs()
    hr()

    cfg = configure_backend()

    hr()
    print(bold("MCP API key") + dim("  (secures the MCP server on :8002 — recommended)"))
    if ask_yes("Generate a strong MCP_API_KEY for you now?", default_yes=True):
        mcp_key = secrets.token_urlsafe(32)
        print(green(f"  ✓ generated: {mcp_key}"))
    else:
        mcp_key = ask("Enter an MCP_API_KEY (blank = leave open, dev only)", "")

    hr()
    faultline_user_id = configure_identity()

    hr()
    write_env(cfg, mcp_key, faultline_user_id)
    print_next_steps(mcp_key)

    print()
    if ask_yes("Build + start the stack now (docker compose up -d --build)?", default_yes=False):
        try:
            subprocess.run(["docker", "compose", "up", "-d", "--build"], cwd=HERE, check=False)
            _poll_health()
        except Exception as e:
            print(red(f"  could not launch docker compose: {e}"))

    print_integration_guide(mcp_key, faultline_user_id)


if __name__ == "__main__":
    main()
