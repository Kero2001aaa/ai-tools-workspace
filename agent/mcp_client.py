"""Connect to the configured local MCP servers and list their tools."""
import asyncio

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.tool_agent import build_workspace_server_config


async def list_workspace_tools() -> None:
    """Start the same allowlisted MCP servers used by the tool-calling agent."""
    load_dotenv()
    client = MultiServerMCPClient(build_workspace_server_config())
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
