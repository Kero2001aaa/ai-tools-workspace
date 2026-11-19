"""Read-only local control plane for the AI workspace.

The dashboard binds to loopback only and exposes no state-changing endpoints.
Keep the Codespaces forwarded port private.
"""
import argparse
import json
import os
import platform
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent.memory_store import MemoryStore
from agent.task_queue import TaskQueue


ROOT = Path(__file__).resolve().parents[1]


def _safe_db_summary(path, table):
    if not path.exists():
        return 0
    import sqlite3
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except (sqlite3.Error, OSError):
        return None


def status_snapshot():
    """Return non-secret operational state; never expose environment values."""
    env = os.environ
    memory_path = Path(env.get("WORKSPACE_MEMORY_DB", ".data/memory.sqlite3")).expanduser()
    task_path = Path(".data/tasks.sqlite3")
    integrations = {
        "Graphify": bool(env.get("GRAPHIFY_GRAPH_PATH")) and Path(env["GRAPHIFY_GRAPH_PATH"]).is_file(),
        "Agent-Reach": env.get("ENABLE_AGENT_REACH_TOOLS", "").lower() in {"1", "true", "yes", "on"},
        "Browser": env.get("ENABLE_BROWSER_TOOLS", "").lower() in {"1", "true", "yes", "on"},
        "Exa": bool(env.get("EXA_API_KEY")),
        "Firecrawl": bool(env.get("FIRECRAWL_API_KEY")),
        "Model endpoint": bool(env.get("MODEL_API_KEY") and env.get("MODEL_BASE_URL") and env.get("MODEL_NAME")),
    }
    return {
        "workspace": "ai-tools-workspace",
        "status": "online",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "root": str(ROOT),
        "integrations": integrations,
        "memory_records": _safe_db_summary(memory_path, "memories"),
        "task_records": _safe_db_summary(task_path, "tasks"),
        "security": {
            "bind": "127.0.0.1",
            "read_only_dashboard": True,
            "secrets_exposed": False,
        },
    }


def task_snapshot(limit=20):
    return TaskQueue().list_tasks(limit)


def memory_snapshot(limit=10):
    return MemoryStore().recent(limit)


def _page():
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Workspace Control Plane</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif;background:#10151d;color:#e8edf5}
body{margin:0 auto;max-width:1100px;padding:32px 20px}h1{margin-bottom:6px}p,.muted{color:#9aa8ba}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:22px 0}
.card{background:#192331;border:1px solid #2a394b;border-radius:12px;padding:18px}
h2{font-size:1rem;margin:0 0 12px}.value{font-size:1.8rem;font-weight:700}
ul{padding-left:20px;line-height:1.8}pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#c6d4e4}
button{background:#26384c;color:#fff;border:0;padding:9px 13px;border-radius:8px;cursor:pointer}
</style></head><body>
<h1>AI Workspace</h1><p>Local control plane · read-only monitoring · no secrets exposed</p>
<div class="grid">
<section class="card"><h2>Workspace</h2><div class="value" id="state">Loading</div><p id="runtime" class="muted"></p></section>
<section class="card"><h2>Memory records</h2><div class="value" id="memories">—</div></section>
<section class="card"><h2>Task records</h2><div class="value" id="tasks">—</div></section>
</div>
<div class="grid">
<section class="card"><h2>Integrations</h2><ul id="integrations"></ul></section>
<section class="card"><h2>Security posture</h2><pre id="security"></pre></section>
</div>
<section class="card"><h2>Recent tasks</h2><pre id="task-list">Loading…</pre></section>
<section class="card"><h2>Recent memory</h2><pre id="memory-list">Loading…</pre></section>
<p class="muted">Refreshes every 30 seconds. The dashboard has no enqueue, execute, delete, or configuration endpoints.</p>
<script>
async function load(){
 const [s,t,m]=await Promise.all([
 fetch('/api/status').then(r=>r.json()),fetch('/api/tasks').then(r=>r.json()),fetch('/api/memories').then(r=>r.json())
 ]);
 document.getElementById('state').textContent=s.status;
 document.getElementById('runtime').textContent='Python '+s.python+' · '+s.checked_at;
 document.getElementById('memories').textContent=s.memory_records??'Unavailable';
 document.getElementById('tasks').textContent=s.task_records??'Unavailable';
 document.getElementById('integrations').innerHTML=Object.entries(s.integrations).map(([k,v])=>'<li>'+k+': '+(v?'enabled':'disabled')+'</li>').join('');
 document.getElementById('security').textContent=JSON.stringify(s.security,null,2);
 document.getElementById('task-list').textContent=JSON.stringify(t,null,2);
 document.getElementById('memory-list').textContent=JSON.stringify(m,null,2);
}
load().catch(e=>{document.getElementById('state').textContent='Error';document.getElementById('task-list').textContent=String(e)});
setInterval(()=>load().catch(()=>{}),30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AIWorkspaceControlPlane/1.0"

    def _send(self, status, body, content_type):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, _page(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, json.dumps(status_snapshot()), "application/json")
        if path == "/api/tasks":
            return self._send(200, json.dumps(task_snapshot(), default=str), "application/json")
        if path == "/api/memories":
            return self._send(200, json.dumps(memory_snapshot(), default=str), "application/json")
        return self._send(404, '{"error":"not found"}', "application/json")

    def do_POST(self):
        self._send(405, '{"error":"read-only control plane"}', "application/json")

    def log_message(self, fmt, *args):
        print("control-plane: " + (fmt % args))


def main():
    parser = argparse.ArgumentParser(description="Run the read-only AI workspace dashboard.")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Port must be between 1024 and 65535.")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Read-only dashboard: http://{args.host}:{args.port}")
    print("Loopback-only listener. Keep the forwarded Codespaces port private.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
