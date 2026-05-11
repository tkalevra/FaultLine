#!/usr/bin/env python3
"""FaultLine MCP Server — entry point.

Launches the Model Context Protocol server on stdin/stdout for Claude integration.
Requires FaultLine API running (default: http://localhost:8001, configurable via
FAULTLINE_API_URL env var or --api-url argument).

Usage:
  python mcp_server.py
  python mcp_server.py --api-url http://192.168.40.10:8001
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
        description="FaultLine MCP Server — exposes knowledge graph tools via stdio"
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="FaultLine API base URL (default: $FAULTLINE_API_URL or http://localhost:8001)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.api_url:
        os.environ["FAULTLINE_API_URL"] = args.api_url

    asyncio.run(run_mcp_server())
