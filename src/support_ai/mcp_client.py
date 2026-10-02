"""MCP-only client boundary for future agents, plus a small diagnostic CLI."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult


class MCPToolError(RuntimeError):
    """A remote tool returned an error or an unexpected response."""


class SupportTools:
    """Holds a connected session; never imports database or policy implementations."""

    def __init__(self, session: ClientSession):
        self.session = session

    async def list_tools(self):
        return (await self.session.list_tools()).tools

    async def call(self, name: str, arguments: dict) -> dict:
        result = await self.session.call_tool(name, arguments)
        if not isinstance(result, CallToolResult):
            raise MCPToolError("Unexpected MCP response type")
        if result.is_error:
            message = (result.structured_content or {}).get("message", "MCP tool rejected the request")
            raise MCPToolError(message)
        if not isinstance(result.structured_content, dict):
            raise MCPToolError("MCP tool returned no structured evidence")
        return result.structured_content


@asynccontextmanager
async def connect_support_tools(*, cwd: Path | None = None, env: dict[str, str] | None = None,
                                timeout: float = 120):
    """Start and clean up a server process in the same interpreter environment.

    Keep this async context and its exit in the same task (SDK task-group rule).
    Explicit env is useful for isolated tests; otherwise inherit process config.
    """
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "support_ai.mcp_server"], cwd=str(cwd or Path.cwd()),
        env=dict(os.environ) if env is None else env)
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
            await session.initialize()
            yield SupportTools(session)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-query", help="Also run a live policy search (OpenAI API usage)")
    args = parser.parse_args()

    async def check():
        async with connect_support_tools() as tools:
            print(json.dumps({"tools": [tool.name for tool in await tools.list_tools()]}))
            print(json.dumps(await tools.call("customer_lookup", {"query": "Emma"}), indent=2))
            if args.policy_query:
                print(json.dumps(await tools.call("policy_search", {"query": args.policy_query}), indent=2))

    try:
        asyncio.run(check())
    except Exception:
        # ExceptionGroups from transport teardown may contain request details.
        parser.exit(1, "MCP check failed; check server logs, configuration, and supplied arguments.\n")


if __name__ == "__main__":
    main()
