"""A minimal, safe-by-default MCP server for the AI workspace.

The server exposes read-only utility tools only. It does not execute shell
commands, read arbitrary files, or make network requests.
"""
import platform

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-tools-workspace")


@mcp.tool()
def text_stats(text: str) -> dict[str, int]:
    """Return basic statistics for supplied text without storing it."""
    return {
        "characters": len(text),
        "words": len(text.split()),
        "lines": len(text.splitlines()) if text else 0,
    }


@mcp.tool()
def workspace_status() -> dict[str, str]:
    """Return non-sensitive runtime information about this workspace."""
    return {
        "workspace": "ai-tools-workspace",
        "python_version": platform.python_version(),
        "runtime": "GitHub Codespaces or compatible Python environment",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
