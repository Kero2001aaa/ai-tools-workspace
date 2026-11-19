"""MCP interface for bounded persistent plans."""
from mcp.server.fastmcp import FastMCP

from agent.plan_store import PlanStore

mcp = FastMCP("ai-tools-workspace-planner")
store = PlanStore()


@mcp.tool()
def create_plan(title: str, steps: list[dict]) -> dict:
    """Create a durable plan of 1-10 approved read-only workspace task steps."""
    return store.create_plan(title, steps)


@mcp.tool()
def get_plan(plan_id: str) -> dict:
    """Retrieve a plan and all its bounded step statuses and results."""
    plan = store.get_plan(plan_id)
    if plan is None:
        raise ValueError("Plan not found")
    return plan


@mcp.tool()
def list_plans(limit: int = 20) -> list[dict]:
    """List recent plans and their statuses."""
    return store.list_plans(limit)


@mcp.tool()
def run_next_plan_step(plan_id: str) -> dict:
    """Execute exactly one pending read-only step in a plan."""
    return store.run_next_step(plan_id)


@mcp.tool()
def retry_failed_plan_step(plan_id: str, step_id: str) -> dict:
    """Explicitly requeue one failed step; execution still requires run_next_plan_step."""
    return store.retry_failed_step(plan_id, step_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
