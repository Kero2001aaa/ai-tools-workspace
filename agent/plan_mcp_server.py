"""MCP interface for bounded persistent plans."""
from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

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
def run_plan_until_done(plan_id: str, max_steps: StrictInt = 3, max_seconds: StrictInt = 60) -> dict:
    """Run a plan autonomously up to bounded step/time budgets; each step is checkpointed and resumable."""
    return store.run_until_done(plan_id, max_steps=max_steps, max_seconds=max_seconds)


@mcp.tool()
def supervise_plan(plan_id: str, max_steps: StrictInt = 3, max_seconds: StrictInt = 60) -> dict:
    """Inspect plan state and make one bounded run, resume-later, wait, or stop decision."""
    return store.supervise(plan_id, max_steps=max_steps, max_seconds=max_seconds)


@mcp.tool()
def retry_failed_plan_step(plan_id: str, step_id: str, retry_reason: str = "explicit_retry_requested") -> dict:
    """Explicitly requeue one failed step; a subsequent run_next_plan_step or authorized run_plan_until_done call is required to execute it."""
    return store.retry_failed_step(plan_id, step_id, retry_reason)


@mcp.tool()
def cancel_plan(plan_id: str, expected_updated_at: str) -> dict:
    """Explicitly cancel only inactive remaining plan work using its last-read updated_at.

    Never interrupts running work or rolls back completed steps. Reload after a
    stale state refusal. Cancelled plans cannot be resumed or retried.
    """
    return store.cancel_plan(plan_id, expected_updated_at)


if __name__ == "__main__":
    mcp.run(transport="stdio")
