"""MCP tools for the local, offline Workspace-scoped SQLite memory store."""
from mcp.server.fastmcp import FastMCP

from agent.memory_store import MemoryStore, ScopedMemoryStore

mcp = FastMCP("ai-tools-workspace-memory")
_store: ScopedMemoryStore | None = None


def _workspace_store() -> ScopedMemoryStore:
    """Resolve the fixed Workspace scope only when a tool is invoked."""
    global _store
    if _store is None:
        _store = MemoryStore().workspace_scope()
    return _store


@mcp.tool()
def remember(content: str, tags: list[str] | None = None) -> dict:
    """Save a concise, non-sensitive fact or project note to local workspace memory."""
    return _workspace_store().remember(content, tags).to_dict()


@mcp.tool()
def recall(query: str, limit: int = 5) -> list[dict]:
    """Find Workspace memories using keyword matching; this is not semantic search."""
    return [record.to_dict() for record in _workspace_store().recall(query, limit)]


@mcp.tool()
def recent_memories(limit: int = 10) -> list[dict]:
    """List recent Workspace memories, newest first."""
    return [record.to_dict() for record in _workspace_store().recent(limit)]


if __name__ == "__main__":
    mcp.run(transport="stdio")
