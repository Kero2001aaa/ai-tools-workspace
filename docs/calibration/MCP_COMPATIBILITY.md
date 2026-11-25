# Direct MCP compatibility pin

The base requirements now explicitly pin mcp==1.30.0, the version already used
in the verified native environment and the optional Graphify requirements.
Local servers directly import mcp.server.fastmcp.FastMCP. A fresh MCP2 install
renamed that API and is not compatible with these existing servers. Depending
only on an unbounded transitive adapter dependency did not preserve that API.

This is a narrow compatibility pin, not a full dependency lock or an assertion
that every installed package has no vulnerabilities. Other requirements remain
as they were, existing CI dependency audits remain in place, and a future MCP
upgrade needs its own API/import/stdio/schema review. No server API or tool
permission is widened and no provider is activated by the pin.

Qualification uses a fresh Python3.12 virtual environment installed from the
base requirements, direct FastMCP import, actual local stdio checkpoint/restart,
strict-intake and memory/recovery tests, followed by the full native test suite.
It does not require or use provider credentials. A normal package installation
contacts the package index only, not a model provider.
