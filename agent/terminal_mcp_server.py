"""Restricted terminal task runner exposed over local STDIO MCP.

No arbitrary shell, user-supplied command strings, or caller-selected paths are
accepted. Every task maps to a fixed executable and argument vector.
"""
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT_CHARS = 12_000
DEFAULT_TIMEOUT_SECONDS = 120

mcp = FastMCP("ai-tools-workspace-terminal")

TASK_COMMANDS = {
    "run_tests": (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"),
    "git_status": ("git", "status", "--short", "--branch"),
    "git_diff_check": ("git", "diff", "--check"),
    "git_log": ("git", "log", "-5", "--oneline", "--decorate"),
    "python_version": (sys.executable, "--version"),
}


def _safe_environment() -> dict[str, str]:
    """Pass only runtime essentials; do not forward API keys or credentials."""
    path = os.environ.get("PATH", "")
    return {
        "PATH": path,
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": os.environ.get("HOME", str(ROOT)),
    }


def execute_workspace_task(task: str) -> dict:
    """Execute one fixed, bounded workspace task; never accepts shell text."""
    if not isinstance(task, str) or task not in TASK_COMMANDS:
        raise ValueError(f"Unsupported task. Allowed tasks: {', '.join(TASK_COMMANDS)}")

    argv = list(TASK_COMMANDS[task])
    if argv[0] == "git":
        import shutil
        executable = shutil.which("git")
        if not executable:
            return {"task": task, "exit_code": 127, "timed_out": False,
                    "output": "git executable is not installed or not on PATH"}
        argv[0] = executable

    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            env=_safe_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        output = result.stdout or ""
        return {
            "task": task,
            "exit_code": result.returncode,
            "timed_out": False,
            "output": output[:MAX_OUTPUT_CHARS],
            "output_truncated": len(output) > MAX_OUTPUT_CHARS,
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {
            "task": task,
            "exit_code": None,
            "timed_out": True,
            "output": str(output)[:MAX_OUTPUT_CHARS],
            "output_truncated": len(str(output)) > MAX_OUTPUT_CHARS,
        }


@mcp.tool()
def run_workspace_task(task: str) -> dict:
    """Run one fixed task: run_tests, git_status, git_diff_check, git_log, or python_version.

    This tool does not provide a general-purpose shell. It cannot execute
    arbitrary commands, accept command arguments, or select filesystem paths.
    """
    return execute_workspace_task(task)


if __name__ == "__main__":
    mcp.run(transport="stdio")
