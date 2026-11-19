"""Local control plane with authenticated, allowlisted task actions.

The dashboard binds to loopback only. Keep the Codespaces forwarded port private.
"""
import argparse
import asyncio
import json
import os
import platform
import secrets
import hmac
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent.memory_store import MemoryStore
from agent.task_queue import ALLOWED_TASK_TYPES, TaskQueue
from agent.task_worker import run_once


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
        "Model endpoint": bool(env.get("MODEL_API_KEY") and env.get("MODEL_NAME")),
        "Dashboard Agent": env.get("ENABLE_DASHBOARD_AGENT", "").lower() in {"1", "true", "yes", "on"},
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
            "read_only_data_endpoints": True,
            "authenticated_task_controls": True,
            "dashboard_agent_opt_in": True,
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
<h1>AI Workspace</h1><p>Local control plane · authenticated task controls · no secrets exposed</p>
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
<section class="card"><h2>AI Agent</h2>
<p class="muted">Runs one explicit request through the configured model and approved MCP tools. Provider charges or free-tier limits may apply. Disabled until ENABLE_DASHBOARD_AGENT=true is set in the private .env file.</p>
<textarea id="agent-request" rows="5" maxlength="4000" placeholder="Describe the research or task for the agent" style="width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #40536a;background:#10151d;color:#fff"></textarea>
<button onclick="runAgent()">Run AI request</button>
<pre id="agent-result" aria-live="polite"></pre></section>
<section class="card"><h2>Controlled task actions</h2>
<p class="muted">Enter the one-time access token printed in the Codespaces terminal. Enqueue and execution are separate actions.</p>
<input id="control-token" type="password" autocomplete="off" placeholder="One-time control token" style="width:min(100%,420px);padding:10px;border-radius:8px;border:1px solid #40536a;background:#10151d;color:#fff">
<select id="task-type" style="padding:10px;margin:8px"><option value="workspace_status">Workspace status</option><option value="memory_recent">Recent memories (read-only)</option></select>
<button onclick="enqueueTask()">Enqueue task</button><button onclick="runTask()">Run one queued task</button>
<pre id="action-result" aria-live="polite"></pre></section>
<p class="muted">Refreshes every 30 seconds. Task execution is limited to the two allowlisted task types; no shell or arbitrary code execution.</p>
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
async function controlPost(path,body){
 const token=document.getElementById('control-token').value;
 if(!token){document.getElementById('action-result').textContent='Enter the one-time token first.';return}
 const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(body)});
 const data=await response.json();
 document.getElementById('action-result').textContent=JSON.stringify(data,null,2);
 if(response.ok) await load();
}
async function runAgent(){
 const request=document.getElementById('agent-request').value.trim();
 if(!request){document.getElementById('agent-result').textContent='Enter a request first.';return}
 if(!confirm('Send this request to the configured model? Model/API usage may incur charges or consume free-tier quota.')) return;
 const token=document.getElementById('control-token').value;
 if(!token){document.getElementById('agent-result').textContent='Enter the one-time token first.';return}
 const output=document.getElementById('agent-result');
 output.textContent='Agent is working…';
 try{
  const response=await fetch('/api/agent/run',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify({request})});
  const data=await response.json();
  output.textContent=JSON.stringify(data,null,2);
 }catch(e){output.textContent=String(e)}
}
async function enqueueTask(){
 if(!confirm('Enqueue this allowlisted task?')) return;
 const type=document.getElementById('task-type').value;
 const payload=type==='memory_recent'?{limit:5}:{};
 try{await controlPost('/api/tasks/enqueue',{task_type:type,payload,max_attempts:1})}
 catch(e){document.getElementById('action-result').textContent=String(e)}
}
async function runTask(){
 if(!confirm('Execute exactly one due task from the allowlist?')) return;
 try{await controlPost('/api/tasks/run-once',{})}
 catch(e){document.getElementById('action-result').textContent=String(e)}
}
load().catch(e=>{document.getElementById('state').textContent='Error';document.getElementById('task-list').textContent=String(e)});
setInterval(()=>load().catch(()=>{}),30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AIWorkspaceControlPlane/1.1"

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.control_token
        if not hmac.compare_digest(supplied, expected):
            self._send(401, '{"error":"authentication required"}', "application/json")
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin:
            parsed = urlparse(origin)
            if parsed.netloc != host or parsed.scheme not in {"http", "https"}:
                self._send(403, '{"error":"cross-origin request rejected"}', "application/json")
                return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site != "same-origin":
            self._send(403, '{"error":"cross-site request rejected"}', "application/json")
            return False
        return True

    def _read_json(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit() or not 1 <= int(raw_length) <= 8192:
            raise ValueError("Request body must be between 1 and 8192 bytes")
        value = json.loads(self.rfile.read(int(raw_length)))
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def _audit(self, action, status, task_id=None):
        path = Path(os.environ.get("CONTROL_AUDIT_DB", ".data/control_audit.sqlite3")).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=3) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS control_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                task_id TEXT
            )""")
            db.execute(
                "INSERT INTO control_audit(occurred_at,action,status,task_id) VALUES(?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), action, status, task_id),
            )

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
        path = urlparse(self.path).path
        if path not in {"/api/tasks/enqueue", "/api/tasks/run-once", "/api/agent/run"}:
            return self._send(405, '{"error":"unsupported action"}', "application/json")
        if not self._authorized():
            return
        with self.server.action_lock:
            now = time.monotonic()
            self.server.action_times = [stamp for stamp in self.server.action_times if now - stamp < 60]
            if len(self.server.action_times) >= 10:
                return self._send(429, '{"error":"action rate limit exceeded"}', "application/json")
            self.server.action_times.append(now)
        try:
            body = self._read_json()
            if path == "/api/agent/run":
                enabled = os.environ.get("ENABLE_DASHBOARD_AGENT", "").strip().lower() in {"1", "true", "yes", "on"}
                if not enabled:
                    return self._send(503, '{"error":"Dashboard Agent is disabled; set ENABLE_DASHBOARD_AGENT=true to opt in"}', "application/json")
                request_text = body.get("request")
                if not isinstance(request_text, str) or not request_text.strip() or len(request_text) > 4000:
                    raise ValueError("request must be a non-empty string of at most 4,000 characters")
                if set(body) != {"request"}:
                    raise ValueError("Only the request field is accepted")
                from agent.tool_agent import run_request
                answer = asyncio.run(run_request(request_text.strip()))
                answer_text = answer if isinstance(answer, str) else str(answer)
                truncated = len(answer_text) > 20000
                self._audit("agent-run", "succeeded")
                return self._send(200, json.dumps({
                    "status": "succeeded",
                    "answer": answer_text[:20000],
                    "truncated": truncated,
                }), "application/json")
            if path == "/api/tasks/enqueue":
                task_type = body.get("task_type")
                payload = body.get("payload", {})
                attempts = body.get("max_attempts", 1)
                if task_type not in ALLOWED_TASK_TYPES:
                    raise ValueError("Task type is not allowlisted")
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be a JSON object")
                if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 3:
                    raise ValueError("max_attempts must be an integer from 1 to 3")
                task = TaskQueue().enqueue(task_type, payload, max_attempts=attempts)
                self._audit("enqueue:" + task_type, "succeeded", task["id"])
                return self._send(201, json.dumps({
                    "status": "queued", "task_id": task["id"], "task_type": task_type,
                }), "application/json")
            if body:
                raise ValueError("run-once accepts an empty JSON object only")
            result = run_once()
            task_id = result.get("task_id")
            status = result.get("status", "unknown")
            self._audit("run-once", status, task_id)
            return self._send(200, json.dumps({
                "status": status, "task_id": task_id,
                "attempts": result.get("attempts"),
                "error": result.get("error"),
            }), "application/json")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send(400, json.dumps({"error": str(exc)}), "application/json")
        except Exception:
            try:
                self._audit("control-action", "failed")
            except Exception:
                pass
            return self._send(500, '{"error":"action failed; inspect local server logs"}', "application/json")

    def log_message(self, fmt, *args):
        print("control-plane: " + (fmt % args))


def main():
    parser = argparse.ArgumentParser(description="Run the authenticated AI workspace control dashboard.")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Port must be between 1024 and 65535.")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.control_token = secrets.token_urlsafe(32)
    server.action_lock = threading.Lock()
    server.action_times = []
    print(f"Dashboard: http://{args.host}:{args.port}")
    print("Loopback-only listener. Keep the forwarded Codespaces port private.")
    print("One-time control token (shown once; do not share): " + server.control_token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
