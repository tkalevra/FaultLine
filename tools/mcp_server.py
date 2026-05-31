#!/usr/bin/env python3
"""FaultLine MCP Server — entry point.

Launches the Model Context Protocol server. Two transport modes are supported:

  stdio  (default) — raw JSON-RPC over stdin/stdout; used by Claude Desktop
  http             — Streamable HTTP via FastAPI/uvicorn; used by Docker/network deployment

Requires FaultLine API running (default: http://localhost:8001, configurable via
FAULTLINE_API_URL env var or --api-url argument).

Usage:
  python mcp_server.py
  python mcp_server.py --api-url http://192.168.1.10:8001
  python mcp_server.py --transport http --port 8002
  python mcp_server.py --transport http --host 0.0.0.0 --port 8002
"""

import argparse
import asyncio
import os
import sys

# Ensure src/ is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.mcp.server import run_mcp_server


def parse_args():
    parser = argparse.ArgumentParser(
        description="FaultLine MCP Server — exposes knowledge graph tools via stdio or HTTP"
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="FaultLine API base URL (default: $FAULTLINE_API_URL or http://localhost:8001)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode: stdio (default, for Claude Desktop) or http (for Docker/network deployment)",
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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.api_url:
        os.environ["FAULTLINE_API_URL"] = args.api_url

    if args.transport == "http":
        import uvicorn
        from src.mcp.http_server import app
        print(f"[faultline-mcp] Starting HTTP transport on {args.host}:{args.port}", file=sys.stderr)
        print(f"[faultline-mcp] FaultLine API: {os.environ.get('FAULTLINE_API_URL', 'http://localhost:8001')}", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        asyncio.run(run_mcp_server())
