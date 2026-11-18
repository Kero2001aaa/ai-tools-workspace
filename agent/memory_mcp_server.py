"""MCP tools for the local, offline SQLite memory store."""
from mcp.server.fastmcp import FastMCP

from agent.memory_store import MemoryStore

mcp = FastMCP("ai-tools-workspace-memory")
store = MemoryStore()


@mcp.tool()
def remember(content: str, tags: list[str] | None = None) -> dict:
    """Save a concise, non-sensitive fact or project note to local workspace memory."""
    return store.remember(content, tags)


@mcp.tool()
def recall(query: str, limit: int = 5) -> list[dict]:
    """Find locally stored memories using keyword matching; this is not semantic search."""
    return store.recall(query, limit)


@mcp.tool()
def recent_memories(limit: int = 10) -> list[dict]:
    """List recent local memories, newest first."""
    return store.recent(limit)


if __name__ == "__main__":
    mcp.run(transport="stdio")
