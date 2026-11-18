"""Tests for safe MCP tool inventory export."""
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import BaseModel

from agent.security_export import tool_inventory, write_tool_inventory


class ExampleArgs(BaseModel):
    query: str


class FakeTool:
    name = "web_search"
    description = "Search public documentation."
    args_schema = ExampleArgs


class SecurityExportTests(unittest.TestCase):
    def test_exports_name_description_and_input_schema(self):
        result = tool_inventory([FakeTool()])
        self.assertEqual(result["tools"][0]["name"], "web_search")
        self.assertEqual(result["tools"][0]["description"], "Search public documentation.")
        self.assertIn("query", result["tools"][0]["inputSchema"]["properties"])

    def test_empty_inventory_is_valid(self):
        self.assertEqual(tool_inventory([]), {"tools": []})

    def test_rejects_missing_tool_name(self):
        class MissingName:
            name = ""
        with self.assertRaisesRegex(ValueError, "non-empty name"):
            tool_inventory([MissingName()])

    def test_rejects_duplicate_tool_names(self):
        class DuplicateTool(FakeTool):
            pass
        with self.assertRaisesRegex(ValueError, "Duplicate MCP tool"):
            tool_inventory([FakeTool(), DuplicateTool()])

    def test_writes_valid_json_and_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "tools.json"
            returned = write_tool_inventory([FakeTool()], output)
            payload = json.loads(returned.read_text(encoding="utf-8"))
            self.assertEqual(payload["tools"][0]["name"], "web_search")

    def test_inventory_does_not_include_environment_secrets(self):
        result = json.dumps(tool_inventory([FakeTool()]))
        self.assertNotIn("API_KEY", result)
        self.assertNotIn("secret", result)


if __name__ == "__main__":
    unittest.main()
