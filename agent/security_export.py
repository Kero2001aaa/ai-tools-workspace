"""Export approved MCP tool metadata for offline security scanning.

Only tool names, descriptions, and JSON input schemas are exported. Runtime
credentials, environment variables, memory contents, and tool results are never
included in this inventory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def tool_inventory(tools: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """Convert LangChain MCP tools into Cisco MCP Scanner's static JSON shape."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        name = str(getattr(tool, "name", "")).strip()
        if not name:
            raise ValueError("Every exported MCP tool must have a non-empty name.")
        if name in seen:
            raise ValueError(f"Duplicate MCP tool name: {name}")
        seen.add(name)

        schema_model = getattr(tool, "args_schema", None)
        if schema_model is None or not hasattr(schema_model, "model_json_schema"):
            schema: dict[str, Any] = {"type": "object", "properties": {}}
        else:
            schema = schema_model.model_json_schema()

        records.append({
            "name": name,
            "description": str(getattr(tool, "description", "") or ""),
            "inputSchema": schema,
        })
    return {"tools": records}


def write_tool_inventory(tools: Iterable[Any], output_path: str | Path) -> Path:
    """Write the tool inventory as UTF-8 JSON and return its resolved path."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = tool_inventory(tools)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
