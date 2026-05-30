"""MCP server — exposes all EvalGov tools over SSE for Claude Code and other MCP clients."""

import structlog
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from tools import TOOL_SCHEMAS, execute_tool

log = structlog.get_logger()

_server = Server("evalgov-agent")


@_server.list_tools()
async def _list_tools() -> list[Tool]:
    return [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["input_schema"],
        )
        for t in TOOL_SCHEMAS
    ]


@_server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    log.info("mcp_tool_called", tool=name)
    result = execute_tool(name, arguments or {})
    return [TextContent(type="text", text=result)]


def build_mcp_app() -> Starlette:
    sse = SseServerTransport("/messages/")

    async def _handle_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as streams:
            await _server.run(
                streams[0], streams[1], _server.create_initialization_options()
            )

    async def sse_endpoint(request: Request) -> Response:
        # Route endpoint wrapper — Route requires a callable that returns Response.
        # Pass request._send (the raw ASGI send) so SseServerTransport writes
        # directly to the connection; root_path stays at /mcp (no Mount prefix added).
        await _handle_sse(request.scope, request.receive, request._send)
        return Response()

    return Starlette(
        routes=[
            Route("/sse", endpoint=sse_endpoint, methods=["GET"]),
            Mount("/messages", app=sse.handle_post_message),
        ]
    )
