"""Restricted project workspace tools exposed over local STDIO MCP.

No arbitrary shell or caller-selected absolute paths are accepted. Terminal
execution uses a fixed task catalog; file access is limited to approved source,
test, and documentation paths inside the repository.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT_CHARS = 12_000
MAX_READ_BYTES = 50_000
MAX_WRITE_BYTES = 100_000
DEFAULT_TIMEOUT_SECONDS = 120
ALLOWED_ROOTS = {"agent", "tests", "docs"}
ALLOWED_ROOT_FILES = {"README.md", "requirements.txt", "requirements-browser.txt", ".env.example"}
DENIED_PARTS = {".git", ".venv", ".data", ".vscode", "__pycache__", "node_modules"}

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
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": os.environ.get("HOME", str(ROOT)),
    }


def _resolve_project_path(relative_path: str) -> Path:
    """Resolve a permitted relative project path and reject symlink escapes."""
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise ValueError("path must be a non-empty relative path")
    if "\\" in relative_path:
        raise ValueError("path must use POSIX separators")
    candidate = Path(relative_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("absolute paths, dot segments, and traversal are not allowed")
    if any(part in DENIED_PARTS or part.startswith(".") for part in candidate.parts):
        if relative_path not in ALLOWED_ROOT_FILES:
            raise ValueError("hidden and protected paths are not allowed")
    if not (candidate.parts[0] in ALLOWED_ROOTS or relative_path in ALLOWED_ROOT_FILES):
        raise ValueError("path is outside the approved project directories")

    current = ROOT
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink paths are not allowed")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("path escapes the project root")
    return resolved


def read_project_file(relative_path: str) -> dict:
    """Read a bounded UTF-8 text file from agent/, tests/, docs/, or approved root files."""
    target = _resolve_project_path(relative_path)
    if not target.is_file():
        raise ValueError("requested project file does not exist or is not a regular file")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"file exceeds the {MAX_READ_BYTES}-byte read limit")
    content = target.read_text(encoding="utf-8")
    return {"path": relative_path, "content": content, "bytes": size}


def write_project_file(relative_path: str, content: str) -> dict:
    """Create or replace one bounded UTF-8 file inside approved project directories."""
    if not isinstance(content, str):
        raise ValueError("content must be text")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise ValueError(f"content exceeds the {MAX_WRITE_BYTES}-byte write limit")
    target = _resolve_project_path(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after directory creation to reject symlinked parent paths.
    target = _resolve_project_path(relative_path)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=".mcp-write-", delete=False
    ) as temporary:
        temporary.write(encoded)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return {
        "path": relative_path,
        "bytes_written": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
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

    No general-purpose shell, arbitrary command arguments, or caller-selected
    working directory is provided.
    """
    return execute_workspace_task(task)


@mcp.tool()
def read_project_file(relative_path: str) -> dict:
    """Read one UTF-8 file under agent/, tests/, docs/, or approved root files.

    Protected paths such as .env, .git, .venv, .data, and .vscode are blocked.
    """
    return read_project_file(relative_path)


@mcp.tool()
def write_project_file(relative_path: str, content: str) -> dict:
    """Create or replace one UTF-8 file under agent/, tests/, docs/, or approved root files.

    Writes are limited to 100 KB per file. Protected paths and symlink escapes
    are rejected. The caller must review the resulting diff before committing.
    """
    return write_project_file(relative_path, content)


if __name__ == "__main__":
    mcp.run(transport="stdio")
