# Restricted Graphify integration boundary

Reviewed on 2026-11-24. This is a code/source review, not a live installation or query report.

## Source and package compatibility

- PyPI graphifyy0.9.118 is available for Python >=3.11.5,<3.15. The downloaded wheel SHA256 is f572453c275e82dcc052f6cf7fa79c0a5a14eaefab08ac9112e31edf7b9d75aa.
- The wheel's serve.py and prs.py exactly match reviewed official commit013a6461c31da6e003c0dea191aca84c94cb8d47. Package metadata and dependency resolution were inspected without installation or execution.
- requirements-graphify.txt pins the optional isolated tool to graphifyy[mcp]==0.9.118 with mcp==1.30.0. Python3.12 dependency resolution succeeded. Without the MCP pin, resolution chose MCP2.3.0; that contract is not tested by this bridge.
- The process checks graphifyy and MCP metadata using the console script's absolute Python interpreter in isolated mode before querying. Other versions, shell/env wrappers and unknown interpreters fail closed. No automatic install, upgrade or repair occurs.
- The current installed Codespace version and live MCP compatibility remain unknown. Unit tests mock upstream protocol responses; they do not prove a deployed installation works.

Sources:
- https://pypi.org/pypi/graphifyy/0.9.118/json
- https://files.pythonhosted.org/packages/1b/06/b3946a2f190bd175ca87afff1e6f26698814dc83ea92459b863f676aef99/graphifyy-0.9.118-py3-none-any.whl
- https://github.com/Graphify-Labs/graphify/blob/013a6461c31da6e003c0dea191aca84c94cb8d47/graphify/serve.py
- https://github.com/Graphify-Labs/graphify/blob/013a6461c31da6e003c0dea191aca84c94cb8d47/graphify/prs.py
- https://github.com/Graphify-Labs/graphify/blob/013a6461c31da6e003c0dea191aca84c94cb8d47/graphify/querylog.py

## Why direct registration was too broad

The reviewed upstream server exposes11 tools, including GitHub PR tools which invoke gh. Every upstream tool accepts project_path to select another project's graph. get_section reads Markdown source, and resources can read graph-adjacent reports. Direct upstream discovery did not enforce the workspace's selected-graph claim. This is an integration-scope mismatch, not a claim of an upstream vulnerability.

## Enforced application boundary

The workspace now registers its own MCP module, not the upstream catalog. It exposes only:

- graphify_graph_stats: graph counts.
- graphify_get_node: one node's stored graph data.
- graphify_query_graph: graph search, depth1-6, output budget100-4000.

Public tools accept no path, project_path, repo, raw command or arbitrary parameter object. Internal dispatch also rejects unknown tools/keys. Queries/labels are bounded to1000 characters. Each call reads at most20MB from the configured regular non-symlink JSON graph, validates nodes/links arrays, then writes a private temporary graph snapshot. No source tree or graph-adjacent sidecar is copied. Upstream resources, get_section and PR tools are never exposed or called.

The upstream child receives a reduced environment, temporary HOME/XDG/cwd, no credentials/proxy/Python-path/git-config inheritance and forced GRAPHIFY_QUERY_LOG_DISABLE=1. The MCP SDK's inherited HOME/LOGNAME/PATH/SHELL/TERM/USER values are explicitly overridden. Query logging is opt-in upstream, but the bridge disables it regardless of parent settings. Temporary snapshots are removed on success, error and timeout.

A20-second async query timeout covers MCP connection, initialization and call. Only text results are accepted, with a40000-character result cap. Upstream errors are returned as bounded generic bridge failures, not raw local paths. No provider/model call is made by bridge tests.

## Limits and security/dependency impact

This is not an OS sandbox. The installed executable and Python interpreter are trusted local programs running as the server user; they can still access that user's filesystem and network. The MCP SDK parses an upstream response before the40000-character bridge result check, so this is not a hostile-process memory ceiling. The timeout relies on SDK subprocess cleanup, not a separate kernel resource quota. The20MB input cap and snapshot do not make arbitrary graph contents trusted instructions.

The optional Graphify package brings a separate dependency tree including NetworkX, NumPy, tree-sitter language grammars and MCP HTTP dependencies. No Graphify dependency is added to base requirements.txt or installed by this change. The pinned top-level package is not a full transitive lock. Base CI scans do not certify an installed optional tool; optional dependency auditing must be recorded separately. A different Graphify version or desired tool surface requires another source/protocol review, not widening the bridge automatically.

Optional dependency evidence: a fresh Python3.12 resolution of requirements-graphify.txt produced60 packages. An OSV querybatch on2026-11-24 returned60 results with zero vulnerability matches for those exact versions. This is a point-in-time advisory check, not a code audit or a guarantee; no optional package was installed or activated. The snapshot is recorded in requirements-graphify-audit.txt for repeatable advisory checks, not as an installed-state claim.
