"""Connect to the local utility, memory, and research MCP servers."""
import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient


async def list_workspace_tools() -> None:
    """Start local stdio servers; browser discovery is opt-in."""
    load_dotenv()
    servers = {
        "workspace": {"transport": "stdio", "command": sys.executable,
                      "args": ["-m", "agent.mcp_server"]},
        "memory": {"transport": "stdio", "command": sys.executable,
                   "args": ["-m", "agent.memory_mcp_server"]},
        "research": {"transport": "stdio", "command": sys.executable,
                     "args": ["-m", "agent.research_mcp_server"]},
    }
    if os.getenv("ENABLE_BROWSER_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}:
        servers["browser"] = {
            "transport": "stdio", "command": sys.executable,
            "args": ["-m", "agent.browser_mcp_server"],
        }
    client = MultiServerMCPClient(servers)
    tools = await client.get_tools()
    if not tools:
        print("MCP servers connected, but exposed no tools.")
        return
    print(f"Connected to local MCP servers. Discovered {len(tools)} tool(s):")
    for tool in tools:
        print(f"- {tool.name}: {tool.description}")


def main() -> None:
    asyncio.run(list_workspace_tools())


if __name__ == "__main__":
    main()
