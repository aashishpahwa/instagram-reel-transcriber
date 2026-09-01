"""The Reel agent: one tool layer over the library, two front doors.

tools.py      - pure functions (user_id, project_id, ...) -> JSON-able dicts
schemas.py    - JSON schemas for those functions (shared by MCP and the in-app runtime)
mcp_server.py - FastMCP server exposing them to Claude Code / Claude Desktop
"""
