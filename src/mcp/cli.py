"""faultline-mcp console script entry point.

Works whether the package is installed (pip install -e .) or run
directly from the repo root. Import paths use the installed package
layout (no src. prefix).
"""

import argparse
import asyncio
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FaultLine MCP Server — exposes knowledge graph tools via stdio or HTTP"
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="FaultLine API base URL (default: $FAULTLINE_API_URL or http://localhost:8000)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode: stdio (default, Claude Desktop) or http (Docker/network)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8002,
        help="Port for HTTP server (default: 8002)",
    )
    args = parser.parse_args()

    if args.api_url:
        os.environ["FAULTLINE_API_URL"] = args.api_url

    if args.transport == "http":
        try:
            import uvicorn
        except ImportError:
            print(
                "HTTP transport requires uvicorn: pip install 'faultline-wgm[mcp-http]'",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            from mcp.http_server import app
        except ImportError:
            from src.mcp.http_server import app  # dev fallback

        api_url = os.environ.get("FAULTLINE_API_URL", "http://localhost:8000")
        print(f"[faultline-mcp] HTTP transport on {args.host}:{args.port}", file=sys.stderr)
        print(f"[faultline-mcp] FaultLine API: {api_url}", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        try:
            from mcp.server import run_mcp_server
        except ImportError:
            from src.mcp.server import run_mcp_server  # dev fallback

        asyncio.run(run_mcp_server())


if __name__ == "__main__":
    main()
