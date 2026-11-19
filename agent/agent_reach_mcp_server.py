"""Public-only MCP bridge for the Agent Reach GitHub and V2EX channels.

Only fixed channel/action pairs are exposed. The CLI runs without shell parsing,
with a temporary HOME, no inherited GitHub credentials, and bounded time/output.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-tools-workspace-agent-reach")

_TIMEOUT_SECONDS = 20
_MAX_OUTPUT_BYTES = 200_000
_QUERY_MAX_CHARS = 300
_ALLOWED = {
    ("github", "search"),
    ("github", "read"),
    ("v2ex", "hot"),
    ("v2ex", "node"),
    ("v2ex", "read"),
    ("v2ex", "user"),
}
_GITHUB_TARGET = re.compile(
    r"^(?:[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}|"
    r"https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}"
    r"(?:/(?:issues|pull)/[0-9]+)?/?(?:[?#].*)?)$"
)
_V2EX_TARGET = re.compile(
    r"^(?:[0-9]{1,12}|https://(?:www\.)?v2ex\.com/t/[0-9]{1,12}/?)$"
)
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _text(value: str, label: str, maximum: int = _QUERY_MAX_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must contain 1 to {maximum} characters.")
    return value


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        raise ValueError("limit must be an integer from 1 to 10.")
    return value


def _run_public(channel: str, action: str, arguments: list[str]) -> dict:
    if (channel, action) not in _ALLOWED:
        raise ValueError("This Agent Reach channel/action is not allowlisted.")
    command = shutil.which(os.getenv("AGENT_REACH_COMMAND", "agent-reach"))
    if not command:
        raise RuntimeError("Agent Reach executable not found. Install the pinned uv tool first.")

    # Isolate Agent Reach config/session state and suppress all inherited credentials.
    with tempfile.TemporaryDirectory(prefix="agent-reach-public-") as home:
        env = os.environ.copy()
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "AGENT_REACH_GITHUB_TOKEN"):
            env.pop(key, None)
        env.update({
            "HOME": home,
            "USERPROFILE": home,
            "NO_COLOR": "1",
            "DO_NOT_TRACK": "true",
            "GH_TELEMETRY": "false",
        })
        argv = [
            command, channel, action, *arguments,
            "--backend", "github-api" if channel == "github" else "v2ex-api",
            "--timeout", "15",
        ]
        try:
            completed = subprocess.run(
                argv, shell=False, cwd=home, env=env,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Agent Reach request timed out.") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not start Agent Reach: {type(exc).__name__}.") from exc

    output = completed.stdout.strip()
    if len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise RuntimeError("Agent Reach response exceeded the 200 KB safety limit.")
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Agent Reach returned invalid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Agent Reach returned an unsupported response schema.")
    if not payload.get("ok"):
        error = payload.get("error") or {}
        code = str(error.get("code", "UNKNOWN"))[:40]
        message = str(error.get("message", "request failed"))[:300]
        raise RuntimeError(f"Agent Reach {code}: {message}")
    return payload


@mcp.tool()
def agent_reach_github_search(query: str, limit: int = 5) -> dict:
    """Search public GitHub repositories using the anonymous GitHub REST API."""
    query = _text(query, "query")
    limit = _limit(limit)
    return _run_public("github", "search", [query, "--limit", str(limit)])


@mcp.tool()
def agent_reach_github_read(target: str) -> dict:
    """Read a public GitHub repository README or a public issue/PR and comments."""
    target = _text(target, "target", 500)
    if not _GITHUB_TARGET.fullmatch(target):
        raise ValueError("target must be owner/repo or a public github.com repository, issue, or PR URL.")
    return _run_public("github", "read", [target])


@mcp.tool()
def agent_reach_v2ex_hot(limit: int = 10) -> dict:
    """Read current public V2EX hot topics."""
    return _run_public("v2ex", "hot", ["--limit", str(_limit(limit))])


@mcp.tool()
def agent_reach_v2ex_node(node_name: str, limit: int = 10) -> dict:
    """Read recent public topics from a V2EX node."""
    node_name = _text(node_name, "node_name", 64)
    if not _NAME.fullmatch(node_name):
        raise ValueError("node_name may contain only letters, numbers, dots, underscores, and hyphens.")
    return _run_public("v2ex", "node", [node_name, "--limit", str(_limit(limit))])


@mcp.tool()
def agent_reach_v2ex_read(target: str) -> dict:
    """Read a public V2EX topic and its available replies."""
    target = _text(target, "target", 200)
    if not _V2EX_TARGET.fullmatch(target):
        raise ValueError("target must be a numeric topic ID or a public v2ex.com/t/<id> URL.")
    return _run_public("v2ex", "read", [target])


@mcp.tool()
def agent_reach_v2ex_user(username: str) -> dict:
    """Read a public V2EX member profile."""
    username = _text(username, "username", 64)
    if not _NAME.fullmatch(username):
        raise ValueError("username may contain only letters, numbers, dots, underscores, and hyphens.")
    return _run_public("v2ex", "user", [username])


if __name__ == "__main__":
    mcp.run(transport="stdio")
