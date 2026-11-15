"""Build a local dev `faultline.mcpb` for Claude Desktop.

There is NO hand-maintained manifest.json or proxy source in this directory any more —
both are GENERATED from `tools/claude_desktop_bundle.py`, the same builder the SaaS
consoles call for the per-seat bundle users actually download. Two hand-maintained copies
is how a manifest fix lands in the copy nobody installs; read the header of that module
before changing anything about the bundle's shape.

Usage (from the repo root, so `src.mcp.tools` resolves for the tool list):

    python3 tools/claude-desktop/build_mcpb.py \\
        --user-id 00000000-0000-4000-8000-000000000000 \\
        --url http://localhost:8002/mcp \\
        --token "$MCP_API_KEY"

Env fallbacks: FAULTLINE_USER_ID, FAULTLINE_MCP_URL, MCP_API_KEY.
"""

import argparse
import json
import os
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from tools.claude_desktop_bundle import build_mcpb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a dev FaultLine .mcpb bundle")
    ap.add_argument("--user-id", default=os.environ.get("FAULTLINE_USER_ID", ""),
                    help="seat UUID (tenant key). Env: FAULTLINE_USER_ID")
    ap.add_argument("--url", default=os.environ.get("FAULTLINE_MCP_URL", "http://localhost:8002/mcp"),
                    help="full MCP endpoint URL. Env: FAULTLINE_MCP_URL")
    ap.add_argument("--token", default=os.environ.get("MCP_API_KEY", ""),
                    help="bearer token. Env: MCP_API_KEY")
    ap.add_argument("--out", default=os.path.join(_HERE, "faultline.mcpb"))
    args = ap.parse_args()

    if not args.user_id:
        ap.error("--user-id (or FAULTLINE_USER_ID) is required — without it every call is "
                 "scoped to the wrong tenant or none at all")
    if not args.token:
        print("WARNING: no --token/MCP_API_KEY — the bundle will send no Authorization "
              "header and will 401 against any MCP server that requires one.",
              file=sys.stderr)

    blob = build_mcpb(args.user_id, args.token, args.url)
    with open(args.out, "wb") as fh:
        fh.write(blob)

    with zipfile.ZipFile(args.out) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
    print(f"Built: {args.out} ({len(blob)} bytes)")
    print(f"  entries          : {names}")
    print(f"  manifest_version : {manifest['manifest_version']}")
    print(f"  server.type      : {manifest['server']['type']}")
    print(f"  command          : {manifest['server']['mcp_config']['command']} "
          f"{manifest['server']['mcp_config']['args']}")
    print(f"  tools listed     : {len(manifest.get('tools', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
