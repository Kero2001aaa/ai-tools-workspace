"""Connect to the local workspace MCP server and list its tools."""
import asyncio
import sys

from langchain_mcp_adapters.client import MultiServerMCPClient


async def list_workspace_tools() -> None:
    """Start the local stdio server and print tools exposed by it."""
    client = MultiServerMCPClient(
        {
            "workspace": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "agent.mcp_server"],
            }
        }
    )
    tools = await client.get_tools()

    if not tools:
        print("MCP server connected, but it exposed no tools.")
        return

    print(f"Connected to local MCP server. Discovered {len(tools)} tool(s):")
    for tool in tools:
        print(f"- {tool.name}: {tool.description}")


def main() -> None:
    asyncio.run(list_workspace_tools())


if __name__ == "__main__":
    main()
