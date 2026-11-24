"""Restricted selected-graph Graphify bridge, disabled unless configured.

Only three graph-data tools are exposed. Each call copies a bounded validated
JSON graph into a fresh directory, so upstream sidecars/source-file tools are
not available. This is an application boundary, not an OS sandbox.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP

from agent.agent_reach_mcp_server import _public_env

mcp = FastMCP("ai-tools-workspace-graphify-restricted")
GRAPHIFY_VERSION = "0.9.118"
MAX_GRAPH_BYTES = 20_000_000
MAX_RESULT_CHARS = 40_000
TIMEOUT_SECONDS = 20
_ALLOWED = {"graph_stats", "get_node", "query_graph"}


def graph_bytes(path):
    """Read only the explicitly configured regular graph, with bounded JSON."""
    source = Path(path)
    if source.is_symlink() or source.suffix != ".json" or not source.is_file():
        raise ValueError("Graphify requires a regular non-symlink JSON graph.")
    try:
        with source.open("rb") as stream:
            raw = stream.read(MAX_GRAPH_BYTES + 1)
    except OSError:
        raise ValueError("Graphify graph cannot be read.") from None
    if len(raw) > MAX_GRAPH_BYTES:
        raise ValueError("Graphify graph exceeds the 20 MB limit.")
    try:
        data = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Graphify graph is invalid JSON.") from None
    if (not isinstance(data, dict) or not isinstance(data.get("nodes"), list)
            or not isinstance(data.get("links", data.get("edges")), list)):
        raise ValueError("Graphify graph must contain nodes and links arrays.")
    return raw


def _interpreter(command):
    """Accept a standard Python console script, never an env/shell wrapper."""
    with open(command, "rb") as script:
        line = script.readline(4096).decode("utf-8", errors="strict").strip()
    if not line.startswith("#!/") or len(line[2:].split()) != 1:
        raise RuntimeError("Graphify command must be a Python console script with an absolute interpreter.")
    python = line[2:]
    if not Path(python).is_file() or not Path(python).name.startswith("python"):
        raise RuntimeError("Graphify console-script interpreter is unsupported.")
    return python


def _verify_version(command, home, env):
    python = _interpreter(command)
    # Isolated mode suppresses user-site/PYTHONPATH. No upstream code is imported.
    result = subprocess.run([python, "-I", "-c",
        "import importlib.metadata as m;print(m.version('graphifyy')+'|'+m.version('mcp'))"],
        cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=5, check=False)
    if result.returncode != 0 or result.stdout.strip() != (GRAPHIFY_VERSION + "|1.30.0").encode():
        raise RuntimeError("Graphify/MCP versions must be exactly " + GRAPHIFY_VERSION + "/1.30.0.")


def _child_env(home):
    env = _public_env(home)
    # Override every default variable inherited by the MCP SDK too.
    env.update({"LOGNAME": "graphify", "USER": "graphify", "SHELL": "/bin/false",
                "TERM": "dumb", "GRAPHIFY_QUERY_LOG_DISABLE": "1",
                "GRAPHIFY_QUERY_LOG_ENABLE": "0", "GRAPHIFY_QUERY_LOG_RESPONSES": "0"})
    return env


async def _invoke_snapshot(command, snapshot, home, env, name, arguments):
    params = StdioServerParameters(command=command, args=[str(snapshot)], env=env, cwd=home)
    # No upstream discovery/resources are returned to the calling model.
    with open(os.devnull, "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=TIMEOUT_SECONDS)) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    if result.isError:
        raise RuntimeError("Graphify query failed.")
    return _result_text(result)


def _result_text(result):
    chunks = []
    size = 0
    for block in result.content:
        if block.type != "text":
            raise RuntimeError("Graphify returned unsupported content.")
        size += len(block.text) + (1 if chunks else 0)
        if size > MAX_RESULT_CHARS:
            raise RuntimeError("Graphify result exceeded the response limit.")
        chunks.append(block.text)
    return "\n".join(chunks)


async def _query(name, arguments):
    if name not in _ALLOWED:
        raise ValueError("Graphify tool is not allowlisted.")
    allowed_keys = {"graph_stats": set(), "get_node": {"label"},
                    "query_graph": {"question", "depth", "token_budget"}}[name]
    if set(arguments) - allowed_keys:
        raise ValueError("Graphify argument is not allowlisted.")
    command = shutil.which(os.environ.get("GRAPHIFY_MCP_COMMAND", "graphify-mcp"))
    path = os.environ.get("GRAPHIFY_GRAPH_PATH", "")
    if not command or not path:
        raise RuntimeError("Graphify restricted bridge is not configured.")
    raw = graph_bytes(path)
    with tempfile.TemporaryDirectory(prefix="graphify-selected-") as home:
        snapshot = Path(home) / "graph.json"
        snapshot.write_bytes(raw)
        env = _child_env(home)
        try:
            await asyncio.to_thread(_verify_version, command, home, env)
            async with asyncio.timeout(TIMEOUT_SECONDS):
                return await _invoke_snapshot(command, snapshot, home, env, name, arguments)
        except Exception:
            raise RuntimeError("Graphify restricted query failed.") from None


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
        raise ValueError("Graphify query must contain 1 to 1000 characters.")
    return value.strip()


@mcp.tool()
async def graphify_graph_stats() -> str:
    """Read statistics from the one configured graph snapshot, without network PR tools."""
    return await _query("graph_stats", {})


@mcp.tool()
async def graphify_get_node(label: str) -> str:
    """Read a node from the configured graph only. No source-file or project-path reads."""
    return await _query("get_node", {"label": _text(label)})


@mcp.tool()
async def graphify_query_graph(question: str, depth: int = 3, token_budget: int = 2000) -> str:
    """Search the configured graph snapshot with bounded depth and output budget."""
    if type(depth) is not int or not 1 <= depth <= 6:
        raise ValueError("depth must be an integer from 1 to 6.")
    if type(token_budget) is not int or not 100 <= token_budget <= 4000:
        raise ValueError("token_budget must be an integer from 100 to 4000.")
    return await _query("query_graph", {"question": _text(question), "depth": depth, "token_budget": token_budget})


if __name__ == "__main__":
    mcp.run(transport="stdio")
