"""MCP tools for the explicit, allowlisted local task queue."""
from mcp.server.fastmcp import FastMCP

from agent.task_queue import ALLOWED_TASK_TYPES, TaskQueue
from agent.task_worker import run_once

mcp = FastMCP("ai-tools-workspace-tasks")
task_queue = TaskQueue()


@mcp.tool()
def enqueue_task(task_type: str, payload: dict | None = None, max_attempts: int = 3) -> dict:
    """Queue one approved local task: workspace_status or memory_recent."""
    if task_type not in ALLOWED_TASK_TYPES:
        raise ValueError(f"Unsupported task type: {task_type}")
    return task_queue.enqueue(task_type, payload, max_attempts=max_attempts)


@mcp.tool()
def run_one_task() -> dict:
    """Execute at most one due task from the approved local queue."""
    return run_once(task_queue)


@mcp.tool()
def list_tasks(limit: int = 20) -> list[dict]:
    """List recent local task records and their execution status."""
    return task_queue.list_tasks(limit)


if __name__ == "__main__":
    mcp.run(transport="stdio")
