"""Optional MCP interface for isolated, tool-less LLM specialist workers."""
from mcp.server.fastmcp import FastMCP

from agent.specialist_worker import run_specialist, validate_delegation

mcp = FastMCP("ai-tools-workspace-specialists")


@mcp.tool()
def delegate_specialist(specialist: str, task: str, context: str = "") -> dict:
    """Delegate bounded analysis to research, coding, or review specialist; no tools or side effects."""
    validate_delegation(specialist, task, context)
    return run_specialist(specialist, task, context)


if __name__ == "__main__":
    mcp.run(transport="stdio")
