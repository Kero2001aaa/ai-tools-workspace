"""Export the workspace's approved MCP tool metadata for static scanning."""
import argparse
import asyncio

from agent.security_export import write_tool_inventory
from agent.tool_agent import load_workspace_tools


async def export(output: str) -> None:
    tools = await load_workspace_tools()
    path = write_tool_inventory(tools, output)
    print(f"Exported {len(tools)} MCP tool definition(s) to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export approved MCP tool definitions without runtime secrets."
    )
    parser.add_argument(
        "--output",
        default=".data/mcp-tools.json",
        help="Output JSON path (default: .data/mcp-tools.json)",
    )
    args = parser.parse_args()
    asyncio.run(export(args.output))


if __name__ == "__main__":
    main()
