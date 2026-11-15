"""Claude Desktop `.mcpb` bundle builder — THE single source of truth.

Both bundle producers call `build_mcpb()` here so a manifest fix can never land in one
copy and miss the door users actually use:

  * `saas/tenant_console.py` → `POST /tenant/seats/{user_id}/claude-desktop-bundle`
    (and the individual console) — the per-seat, credential-baked bundle a real user
    downloads from the "Connect Claude Desktop" button. THIS is the door that matters.
  * `tools/claude-desktop/build_mcpb.py` — the local dev bundle.

Public/AGPL core: this module knows nothing about accounts, seats or billing — it takes
(user_id, token, mcp_url) and returns ZIP bytes, so `saas/` may import it (saas → src/tools
is the allowed direction; nothing here imports `saas`).

────────────────────────────────────────────────────────────────────────────────────────
WHY THE BUNDLE IS SHAPED THIS WAY — do not "simplify" any of the three rules below.

Claude Desktop ships its OWN Node.js runtime; users install nothing:
  "We ship Node.js with Claude Desktop, eliminating external dependencies."
      — https://www.anthropic.com/engineering/desktop-extensions
  "Node.js is strongly recommended: Ships with Claude Desktop on macOS and Windows, so
   users need no separate runtime."
      — https://claude.com/docs/connectors/building/mcpb

But the built-in runtime is only CREDITED to a bundle whose launch command is literally
`node`. From the extension compatibility checker (`NPt`) as decompiled in
https://github.com/anthropics/claude-code/issues/35175:

    const r = t.server.mcp_config.command;
    const s = t.compatibility?.runtimes?.node;
    if (n /* builtInNodeVersion */) {
        const c = r && /^node(\\.exe)?$/.test(r);   //  ← command must BE "node"
        const l = !s || semver.satisfies(n, s);
        ...
    }

RULE 1 — `mcp_config.command` MUST be exactly `"node"`.
    A bundle launched with `npx` (e.g. wrapping `mcp-remote`) does not match that regex,
    so the built-in runtime cannot satisfy it and Claude Desktop falls through to
    requiring a SYSTEM Node.js — which a normal Windows machine does not have. That is
    the "unmet requirement: Node.js, won't install" report. `npx` is npm's launcher; the
    bundled runtime provides `node`, not `npx`.

RULE 2 — ZERO dependencies, no `node_modules`, no network fetch at launch.
    `server/index.js` below is a self-contained stdio↔HTTP JSON-RPC bridge built on
    `node:http`/`node:https` only. Nothing to install, nothing to resolve from a registry.

RULE 3 — `manifest_version` must be one the INSTALLED Claude Desktop understands, which
    LAGS the published spec. A too-new value is rejected at install with all requirements
    showing green and the Install button disabled ("This extension requires an update to
    Claude Desktop") — https://github.com/modelcontextprotocol/mcpb/issues/143, where an
    MCPB maintainer confirms the CLI deliberately generates an OLDER version than the spec
    as a client-lag buffer. `0.3` is the spec's stated current version and the version the
    repo's `schemas/mcpb-manifest-latest.schema.json` pins. **`0.4` exists in the repo as a
    schema file (it adds `server.type: "uv"`) but is NOT what `latest` resolves to** —
    validating against `src/schemas/0.4.ts` because it is the highest-numbered file is
    exactly the trap issue #143 describes. If an install preview ever says "requires an
    update to Claude Desktop", step _MCPB_MANIFEST_VERSION DOWN, never up.

NOT VIABLE TODAY — connecting Claude Desktop straight to the hosted FaultLine URL as a
remote connector (which would delete this bundle entirely). Per
https://claude.com/docs/connectors/building/authentication the hosted Claude surfaces
(web, Desktop, mobile, Cowork) share one connector backend that supports OAuth
(DCR/CIMD/Anthropic-held) or `none`; a fixed bearer token is `static_headers`, which is
BETA and entered once by an ORG ADMIN for the whole organisation — it cannot carry a
per-seat `flu_` token. That page also states Anthropic's outbound traffic originates from
`160.79.104.0/21`, i.e. the connector is dialled from Anthropic's network, so a LAN
pre-prod host is unreachable. Revisit only if FaultLine grows an OAuth 2.1 authorization
server; until then the local bundle is the only per-seat path.
"""

from __future__ import annotations

import io
import json
import zipfile

__all__ = ["build_mcpb", "build_manifest", "build_bridge_js", "MCPB_MANIFEST_VERSION"]

# See RULE 3. Lower this (→ "0.2") if a client rejects the bundle; never raise it
# speculatively.
MCPB_MANIFEST_VERSION = "0.3"

# Read timeout for a single JSON-RPC round trip. Ingest of a long turn is slow; a short
# timeout reads to the user as "the memory is broken".
_BRIDGE_TIMEOUT_MS = 90_000


def _tool_summaries() -> list[dict]:
    """The install preview's tool list, resolved from the ONE source of truth
    (`src/mcp/tools.py::TOOLS` — the same object `POST /mcp` `tools/list` returns).

    Never hand-write tool copy here: three hand-maintained copies of these descriptions
    have already drifted once on this project, and the door users actually read served the
    stale one. Degrades to no tool list rather than to a stale one.
    """
    try:
        from src.mcp.tools import TOOLS  # noqa: PLC0415 - optional, import-time cost only
    except Exception:  # pragma: no cover - packaging fallback
        return []
    out = []
    for tool in TOOLS:
        name = tool.get("name")
        if not name:
            continue
        entry = {"name": name}
        desc = tool.get("description")
        if desc:
            entry["description"] = desc
        out.append(entry)
    return out


def build_manifest(user_id: str, mcp_url: str) -> dict:
    """The manifest.json body. Credentials are NOT placed here — they are baked into
    `server/index.js` so the secret does not get copied into Claude Desktop's config file
    or into a process command line visible to every other process on the machine."""
    manifest: dict = {
        "manifest_version": MCPB_MANIFEST_VERSION,
        "name": "faultline-memory",
        "display_name": "FaultLine Memory",
        "version": "1.0.0",
        "description": (
            "FaultLine write-validated memory — connects this Claude Desktop seat to your "
            "hosted FaultLine MCP server. Credentials are baked in; nothing to install."
        ),
        "author": {"name": "FaultLine"},
        "server": {
            "type": "node",
            "entry_point": "server/index.js",
            # RULE 1: command is literally "node" so Claude Desktop's BUILT-IN Node.js
            # runtime satisfies it and no system Node.js is required.
            "mcp_config": {
                "command": "node",
                "args": ["${__dirname}/server/index.js"],
            },
        },
        "compatibility": {
            # Claude Desktop runs on macOS and Windows; linux is listed for other MCPB
            # hosts. The platform list only ever REJECTS a platform that is absent.
            "platforms": ["darwin", "win32", "linux"],
            # Checked against the BUILT-IN runtime because the command is "node" (see NPt
            # above): `!s || satisfies(builtInNodeVersion, s)`. Electron's embedded Node is
            # well past 18, so this is satisfied without anything installed.
            "runtimes": {"node": ">=18.0.0"},
        },
    }
    tools = _tool_summaries()
    if tools:
        manifest["tools"] = tools
        manifest["tools_generated"] = False
    return manifest


def build_bridge_js(user_id: str, token: str, mcp_url: str) -> str:
    """A dependency-free Node stdio↔HTTP MCP bridge with this seat's credentials baked in.

    Reads newline-delimited JSON-RPC on stdin, POSTs each message to FaultLine's
    `POST /mcp` with `Authorization: Bearer <flu_token>` + `X-OpenWebUI-User-Id`, writes
    the reply back on stdout. Notifications (no `id`) are forwarded and their reply
    dropped, per JSON-RPC. Diagnostics go to stderr ONLY — anything on stdout that is not
    a JSON-RPC message corrupts the transport.

    An HTTP failure is surfaced as a JSON-RPC error carrying the status and body, so a
    missing/short `MCP_API_KEY` reads as a clean 401 instead of a silent disconnect.
    """
    # json.dumps produces an ASCII-only, valid JS string literal for every input.
    return f"""#!/usr/bin/env node
// FaultLine remote bridge — generated per seat. Contains a live credential: do not share.
// Zero dependencies on purpose: runs on the Node.js runtime bundled with Claude Desktop,
// so the user installs nothing. See tools/claude_desktop_bundle.py for why.
'use strict';
const http = require('node:http');
const https = require('node:https');

const MCP_URL = {json.dumps(mcp_url)};
const TOKEN = {json.dumps(token)};
const USER_ID = {json.dumps(user_id)};
const TIMEOUT_MS = {_BRIDGE_TIMEOUT_MS};

const target = new URL(MCP_URL);
const client = target.protocol === 'https:' ? https : http;
const PORT = target.port || (target.protocol === 'https:' ? 443 : 80);

function log(m) {{
  try {{ process.stderr.write('[faultline] ' + m + '\\n'); }} catch (e) {{ /* stderr gone */ }}
}}
log('bridge up -> ' + target.origin + target.pathname
    + ' user=' + (USER_ID ? USER_ID.slice(0, 8) + '…' : 'NONE')
    + ' auth=' + (TOKEN ? 'bearer(' + TOKEN.length + ')' : 'NONE'));

function reply(obj) {{
  try {{ process.stdout.write(JSON.stringify(obj) + '\\n'); }} catch (e) {{ log('stdout write failed: ' + e); }}
}}

function errorFor(msg, code, message) {{
  return {{ jsonrpc: '2.0', error: {{ code: code, message: message }},
           id: (msg && msg.id !== undefined) ? msg.id : null }};
}}

function forward(msg, done) {{
  // The seat's UUID is the tenant key; inject it when the client did not supply one.
  try {{
    const a = msg && msg.params && msg.params.arguments;
    if (USER_ID && a && typeof a === 'object' && a.user_id === undefined) a.user_id = USER_ID;
  }} catch (e) {{ /* leave the message untouched */ }}

  const body = Buffer.from(JSON.stringify(msg), 'utf8');
  const headers = {{
    'Content-Type': 'application/json',
    'Content-Length': body.length,
    'Accept': 'application/json'
  }};
  if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  if (USER_ID) headers['X-OpenWebUI-User-Id'] = USER_ID;

  const req = client.request({{
    protocol: target.protocol,
    hostname: target.hostname,
    port: PORT,
    path: target.pathname + target.search,
    method: 'POST',
    headers: headers
  }}, function (res) {{
    const chunks = [];
    res.on('data', function (c) {{ chunks.push(c); }});
    res.on('end', function () {{
      const text = Buffer.concat(chunks).toString('utf8');
      if (res.statusCode < 200 || res.statusCode >= 300) {{
        log('HTTP ' + res.statusCode + ' from FaultLine');
        return done(errorFor(msg, res.statusCode,
          'FaultLine MCP returned HTTP ' + res.statusCode + ': ' + text.slice(0, 500)));
      }}
      let parsed;
      try {{ parsed = JSON.parse(text); }}
      catch (e) {{ return done(errorFor(msg, -32700, 'invalid JSON from FaultLine: ' + text.slice(0, 200))); }}
      done(parsed);
    }});
  }});

  req.setTimeout(TIMEOUT_MS, function () {{
    req.destroy(new Error('no response within ' + TIMEOUT_MS + 'ms'));
  }});
  req.on('error', function (e) {{
    done(errorFor(msg, -32000, 'cannot reach FaultLine at ' + target.origin + ': ' + (e && e.message ? e.message : e)));
  }});
  req.end(body);
}}

// In-flight accounting: stdin closing must NOT kill requests that are still in the air.
// Claude Desktop holds stdin open, but a piped/scripted run closes it immediately, and
// exiting there would silently drop an already-accepted write (a lost memory).
let inFlight = 0;
let stdinClosed = false;
function maybeExit() {{ if (stdinClosed && inFlight === 0) process.exit(0); }}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', function (chunk) {{
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf('\\n')) >= 0) {{
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try {{ msg = JSON.parse(line); }}
    catch (e) {{ log('skipping non-JSON stdin line'); continue; }}
    // A notification has a method and no id; JSON-RPC forbids replying to it.
    const isNotification = msg && msg.method !== undefined && msg.id === undefined;
    inFlight += 1;
    (function (m, silent) {{
      forward(m, function (resp) {{
        if (!silent) {{
          if (resp && (resp.id === undefined || resp.id === null) && m.id !== undefined) resp.id = m.id;
          reply(resp);
        }}
        inFlight -= 1;
        maybeExit();
      }});
    }})(msg, isNotification);
  }}
}});
process.stdin.on('end', function () {{ stdinClosed = true; maybeExit(); }});
process.on('uncaughtException', function (e) {{ log('uncaught: ' + (e && e.stack ? e.stack : e)); }});
"""


def build_mcpb(user_id: str, token: str, mcp_url: str) -> bytes:
    """Assemble the `.mcpb` (a ZIP with manifest.json at its root) in memory.

    `token` is a live secret: it is written INTO the bundle and must never be logged.
    """
    manifest = build_manifest(user_id, mcp_url)
    bridge = build_bridge_js(user_id, token, mcp_url)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("server/index.js", bridge)
    return buf.getvalue()
