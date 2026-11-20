"""Optional MCP interface for isolated specialists and bounded supervision."""
from mcp.server.fastmcp import FastMCP

from agent.specialist_worker import run_specialist, validate_delegation
from agent.supervisor_worker import supervise_request

mcp = FastMCP("ai-tools-workspace-specialists")


@mcp.tool()
def delegate_specialist(specialist: str, task: str, context: str = "") -> dict:
    """Delegate bounded analysis to research, coding, or review specialist; no tools or side effects."""
    validate_delegation(specialist, task, context)
    return run_specialist(specialist, task, context)


@mcp.tool()
def supervise_request(request: str, context: str = "") -> dict:
    """Run a bounded supervisor workflow: one plan, up to two specialists, then one final review; no retries or side effects."""
    return supervise_request_impl(request, context)


def supervise_request_impl(request: str, context: str = "") -> dict:
    return supervise_request_worker(request, context)


from agent.supervisor_worker import supervise_request as supervise_request_worker


if __name__ == "__main__":
    mcp.run(transport="stdio")
