"""Public-only MCP bridge for the Agent Reach GitHub and V2EX channels.

Only fixed channel/action pairs are exposed. The CLI runs without shell parsing,
with a temporary HOME, an allowlisted environment, and bounded time/combined output.
POSIX process groups are required. This bridge is not an OS sandbox.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import selectors
import signal
import time

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
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _text(value: str, label: str, maximum: int = _QUERY_MAX_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if not value or value.startswith("-") or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must contain 1 to {maximum} characters.")
    return value


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        raise ValueError("limit must be an integer from 1 to 10.")
    return value


def _public_env(home):
    # Deliberately do not inherit model/cloud credentials, proxies, Python paths,
    # git config, custom certificate hooks, or user session/config directories.
    return {
        "PATH": os.defpath,
        "HOME": home, "USERPROFILE": home,
        "XDG_CONFIG_HOME": home, "XDG_DATA_HOME": home,
        "XDG_CACHE_HOME": home, "XDG_STATE_HOME": home,
        "TMPDIR": home, "TEMP": home, "TMP": home,
        "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1", "NO_COLOR": "1",
        "DO_NOT_TRACK": "true", "GH_TELEMETRY": "false",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _stop_process(process):
    # A child can spawn descendants holding a pipe open. Kill the session group
    # on POSIX, even if the direct child already exited. This is not a sandbox.
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def _capture_bounded(argv, home, env):
    """Bound combined stdout/stderr while reading, including stderr floods."""
    if os.name != "posix":
        raise RuntimeError("Public Agent Reach requires POSIX process-group support.")
    try:
        process = subprocess.Popen(argv, shell=False, cwd=home, env=env,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as exc:
        raise RuntimeError("Could not start Agent Reach.") from None
    output = bytearray()
    total = 0
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Agent Reach request timed out.")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), min(8192, _MAX_OUTPUT_BYTES - total + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > _MAX_OUTPUT_BYTES:
                        raise RuntimeError("Agent Reach response exceeded the 200 KB safety limit.")
                    if key.data:
                        output.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Agent Reach request timed out.")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise RuntimeError("Agent Reach request timed out.") from None
        return output.decode("utf-8", errors="replace"), returncode
    finally:
        _stop_process(process)
        process.stdout.close()
        process.stderr.close()


def _run_public(channel: str, action: str, arguments: list[str]) -> dict:
    if (channel, action) not in _ALLOWED:
        raise ValueError("This Agent Reach channel/action is not allowlisted.")
    command = shutil.which(os.getenv("AGENT_REACH_COMMAND", "agent-reach"))
    if not command:
        raise RuntimeError("Agent Reach executable not found. Install the pinned uv tool first.")

    # Isolate Agent Reach config/session state and suppress all inherited credentials.
    with tempfile.TemporaryDirectory(prefix="agent-reach-public-") as home:
        env = _public_env(home)
        argv = [
            command, channel, action, *arguments,
            "--backend", "github-api" if channel == "github" else "v2ex-api",
            "--timeout", "15",
        ]
        output, returncode = _capture_bounded(argv, home, env)

    output = output.strip()
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Agent Reach returned invalid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Agent Reach returned an unsupported response schema.")
    if not payload.get("ok"):
        error = payload.get("error") or {}
        if not isinstance(error, dict):
            raise RuntimeError("Agent Reach returned an invalid error response.")
        code = str(error.get("code", "UNKNOWN"))[:40]
        message = str(error.get("message", "request failed"))[:300]
        raise RuntimeError(f"Agent Reach {code}: {message}")
    if returncode != 0:
        raise RuntimeError("Agent Reach exited unsuccessfully.")
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
