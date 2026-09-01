"""MCP server: the Reel agent's tools for Claude Code / Claude Desktop.

    python -m agent.mcp_server            # stdio transport

Scoping: an MCP session is one user + one project, set from the environment:
    MCP_USER_ID     - the app_users.id (uuid) to act as (required)
    MCP_PROJECT_ID  - the project to work in (required)
plus the app's own DATABASE_URL etc. (.env is loaded from the repo root). The
web app's per-user AI settings are used by the few tools that call a model
(tag_text).

Register in Claude Code (run from the repo root, with the venv's python):
    claude mcp add reel-agent -e MCP_USER_ID=<uuid> -e MCP_PROJECT_ID=<id> -- <venv>/python -m agent.mcp_server

Built on the low-level `mcp.server.Server` (mcp >= 2.0, callback style) rather
than a decorator framework, so the tool schemas the model sees are exactly
agent/schemas.py - one source of truth shared with the in-app runtime - instead
of being inferred from function signatures.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import mcp.types as types  # noqa: E402
from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

from agent import prompts, schemas, tools  # noqa: E402

USER_ID = os.environ.get("MCP_USER_ID", "").strip()
PROJECT_ID = os.environ.get("MCP_PROJECT_ID", "").strip()
if not USER_ID or not PROJECT_ID.isdigit():
    print("MCP_USER_ID and MCP_PROJECT_ID must be set (see agent/mcp_server.py docstring)", file=sys.stderr)
    sys.exit(2)
PROJECT_ID = int(PROJECT_ID)

OVERVIEW_URI = "reel://overview"


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=[
        types.Tool(name=s["name"], description=s["description"], input_schema=s["input_schema"])
        for s in schemas.TOOL_SCHEMAS if s["name"] in tools.TOOL_FUNCTIONS
    ])


async def on_call_tool(ctx, params):
    # Tool bodies are synchronous DB/model calls; run them off the event loop so
    # a slow model call can't stall the MCP session.
    result = await asyncio.to_thread(tools.call_tool, params.name, USER_ID, PROJECT_ID, params.arguments or {})
    is_error = isinstance(result, dict) and set(result.keys()) == {"error"}
    return types.CallToolResult(content=[types.TextContent(type="text", text=_dumps(result))], is_error=is_error)


async def on_list_prompts(ctx, params):
    return types.ListPromptsResult(prompts=[
        types.Prompt(name="reel-agent", description="Operate as the Reel agent for this project.", arguments=[])
    ])


async def on_get_prompt(ctx, params):
    if params.name != "reel-agent":
        raise ValueError(f"unknown prompt {params.name}")
    return types.GetPromptResult(
        description="Reel agent operating instructions",
        messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=prompts.SYSTEM_PROMPT))],
    )


async def on_list_resources(ctx, params):
    return types.ListResourcesResult(resources=[
        types.Resource(uri=OVERVIEW_URI, name="Project overview", mime_type="application/json",
                       description="Counts, baselines, playbook and notes for the active project.")
    ])


async def on_read_resource(ctx, params):
    if str(params.uri) != OVERVIEW_URI:
        raise ValueError(f"unknown resource {params.uri}")
    data = await asyncio.to_thread(tools.get_project_overview, USER_ID, PROJECT_ID)
    return types.ReadResourceResult(contents=[
        types.TextResourceContents(uri=OVERVIEW_URI, mime_type="application/json", text=_dumps(data))
    ])


server = Server(
    "reel-agent",
    instructions=prompts.SYSTEM_PROMPT,
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
    on_list_prompts=on_list_prompts,
    on_get_prompt=on_get_prompt,
    on_list_resources=on_list_resources,
    on_read_resource=on_read_resource,
)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
