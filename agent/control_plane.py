"""Local control plane with authenticated reads and allowlisted task actions.

The dashboard binds to loopback only. Keep the Codespaces forwarded port private.
"""
import argparse
import asyncio
import json
import os
import platform
import re
import secrets
import hmac
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

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
            "authenticated_monitoring": True,
            "host_allowlist": True,
            "dashboard_agent_opt_in": True,
            "secrets_exposed": False,
        },
    }


def task_snapshot(limit=20):
    return TaskQueue().list_tasks(limit)


_ADULT_REASON_CODES = frozenset({
    "initial", "step_completed", "approval_required", "manual_yield", "lease_recovered",
    "needs_input", "paused", "cancelled", "worker_released", "yielded",
    "cancel_observed", "budget_exhausted",
})
_ADULT_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _adult_store():
    """Open the Adult store only if it already exists; a status read never creates it."""
    from agent.adult.adult_task_store import AdultTaskStore, DEFAULT_DB_PATH
    configured = os.environ.get("ADULT_STATE_DB")
    path = Path(configured) if configured else DEFAULT_DB_PATH
    if not path.is_file():
        return None
    return AdultTaskStore(path, read_only=True)


def _adult_task_view(task):
    """Allowlisted lifecycle fields only: no fingerprints, payload/artifact ids or metadata."""
    return {
        "task_id": task.task_id, "task_type": task.task_type, "status": task.status,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "state_version": task.state_version, "attempt_count": task.attempt_count,
        "failed_attempts_since_approval": task.failed_attempts_since_approval,
        "provider_calls": task.provider_calls, "max_total_attempts": task.max_total_attempts,
        "max_provider_calls": task.max_provider_calls, "spent_cost_microusd": task.spent_cost_microusd,
        "deadline_at": task.deadline_at, "has_result": task.latest_result_artifact_id is not None,
        "last_error_code": task.last_error_code, "user_action_required": task.user_action_required,
        "cancel_requested": task.cancel_requested,
    }


def _adult_event_view(event):
    reason = event.metadata.get("reason_code") if isinstance(event.metadata, dict) else None
    return {
        "event_type": event.event_type, "from_status": event.from_status,
        "to_status": event.to_status, "created_at": event.created_at,
        "reason_code": reason if isinstance(reason, str) and reason in _ADULT_REASON_CODES else None,
    }


def _adult_checkpoint_view(checkpoint):
    if checkpoint is None:
        return None
    snap = checkpoint.snapshot
    return {
        "revision": checkpoint.revision, "reason": checkpoint.reason,
        "created_at": checkpoint.created_at, "status": snap.status,
        "user_action_required": checkpoint.user_action_required,
        "completed_steps": len(snap.completed_step_ids), "pending_steps": len(snap.pending_step_ids),
        "total_attempts": snap.total_attempts, "remaining_attempts": snap.remaining_attempts,
        "remaining_wall_seconds": snap.remaining_wall_seconds,
        "last_error_code": snap.last_error_code, "allowed_decisions": list(snap.allowed_decisions),
    }


def adult_tasks_snapshot(limit=20, status=None):
    store = _adult_store()
    if store is None:
        return {"available": False, "tasks": []}
    page = store.list_tasks(status=status, limit=limit)
    tasks = []
    for task in page.items:
        view = _adult_task_view(task)
        events = store.list_events(task.task_id, limit=1, newest_first=True)
        view["latest_event"] = _adult_event_view(events[0]) if events else None
        view["latest_checkpoint"] = _adult_checkpoint_view(store.get_latest_checkpoint(task.task_id))
        tasks.append(view)
    return {"available": True, "tasks": tasks, "has_more": page.has_more}


def adult_task_detail(task_id):
    store = _adult_store()
    task = store.get_task(task_id) if store is not None else None
    if task is None:
        return None
    view = _adult_task_view(task)
    view["events"] = [_adult_event_view(e) for e in reversed(store.list_events(task_id, limit=50, newest_first=True))]
    view["latest_checkpoint"] = _adult_checkpoint_view(store.get_latest_checkpoint(task_id))
    return view


def memory_snapshot(limit=10):
    return MemoryStore().recent(limit)


def allowed_authorities(port, env=None):
    """Exact local and Codespaces authorities, never a wildcard proxy trust."""
    env = os.environ if env is None else env
    values = {f"127.0.0.1:{port}", f"localhost:{port}"}
    name = env.get("CODESPACE_NAME", "")
    domain = env.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    import re
    if re.fullmatch(r"[a-zA-Z0-9-]+", name) and re.fullmatch(r"[a-zA-Z0-9.-]+", domain):
        authority = f"{name}-{port}.{domain}".lower()
        values.update({authority, authority + ":443"})
    return values


def _page():
    return """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Workspace Control Plane · لوحة مساحة العمل</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif;background:#10151d;color:#e8edf5}
body{margin:0 auto;max-width:1100px;padding:32px 20px}h1{margin-bottom:6px}p,.muted{color:#9aa8ba}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:22px 0}
.card{background:#192331;border:1px solid #2a394b;border-radius:12px;padding:18px}
h2{font-size:1rem;margin:0 0 12px}.value{font-size:1.8rem;font-weight:700}
ul{padding-left:20px;line-height:1.8}pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#c6d4e4}
pre,input{direction:ltr;text-align:left;unicode-bidi:plaintext}pre{font-size:0.85rem;line-height:1.6}label{display:block;margin-top:10px}button,select{margin-block:5px;max-width:100%}ul{padding-inline-start:20px}bdi{unicode-bidi:isolate}
button{background:#26384c;color:#fff;border:0;padding:9px 13px;border-radius:8px;cursor:pointer}
</style></head><body>
<h1>مساحة العمل <bdi>AI Workspace</bdi></h1><p>لوحة محلية خاصة · المتابعة والإجراءات تحتاج رمز دخول · لا تعرض قيم الأسرار</p>
<section class="card"><h2>فتح المتابعة</h2>
<p class="muted">أدخل رمز الدخول الظاهر في الطرفية. مطلوب للمتابعة الخاصة والإجراءات، ولا يُحفظ بعد مغادرة الصفحة.</p>
<input id="control-token" type="password" autocomplete="off" placeholder="رمز الدخول" aria-label="رمز الدخول" style="width:min(100%,420px);box-sizing:border-box;padding:10px">
<button onclick="load().catch(showLoadError)">تحميل البيانات الخاصة</button></section>
<p class="muted">دليل الحالة: <bdi>online</bdi> متصل · <bdi>Locked</bdi> مقفل · <bdi>completed</bdi> مكتمل · <bdi>cancelled</bdi> ملغى. تبقى معرّفات المهام والحالات التقنية والنتائج بلغتها الأصلية دون ترجمة.</p>
<div class="grid">
<section class="card"><h2>متابعة مساحة العمل</h2><div class="value" id="state">بانتظار التحميل</div><p id="runtime" class="muted"></p></section>
<section class="card"><h2>سجلات الذاكرة</h2><div class="value" id="memories">—</div></section>
<section class="card"><h2>سجلات المهام</h2><div class="value" id="tasks">—</div></section>
</div>
<div class="grid">
<section class="card"><h2>التكاملات</h2><ul id="integrations"></ul></section>
<section class="card"><h2>إعدادات الأمان</h2><pre id="security"></pre></section>
</div>
<section class="card"><h2>آخر المهام</h2><pre id="task-list">بانتظار التحميل</pre></section>
<section class="card"><h2>الخطط العامة</h2><p class="muted">عرض حالة الخطط للقراءة فقط. العمل الجاري ذو النتيجة المجهولة ينتظر التحقق، ولا يُعاد تلقائيًا.</p><pre id="plan-list" aria-live="polite">مقفل</pre></section>
<section class="card"><h2>تقرير اقتباسات الكود المحفوظ</h2>
<p class="muted">اعرض تقريرًا محليًا مكتملًا من اقتباسات فقط. لا يقرأ المصدر ولا ينفذ مهمة أو يستدعي نموذجًا. نسخة محفوظة كنص غير مشفر، وليست الكود الحالي أو فهمًا بنموذج.</p>
<label>معرّف الخطة <bdi>Plan ID</bdi> <input id="report-plan" maxlength="36" autocomplete="off" style="width:100%;box-sizing:border-box;padding:8px"></label>
<label>معرّف الخطوة <bdi>Step ID</bdi> <input id="report-step" maxlength="36" autocomplete="off" style="width:100%;box-sizing:border-box;padding:8px"></label>
<button onclick="loadSavedReport()">عرض التقرير المحفوظ</button>
<pre id="saved-report" aria-live="polite">مقفل</pre></section>
<section class="card"><h2>آخر الذكريات</h2><pre id="memory-list">بانتظار التحميل</pre></section>
<section class="card"><h2>طلب للمساعد</h2>
<p class="muted">ينفذ طلبًا صريحًا واحدًا بالنموذج المُعدّ والأدوات المسموحة. قد تُستهلك حصة مجانية؛ لا يُسمح بمسار مدفوع تلقائي. معطل حتى يفعّله المشغّل في الإعداد الخاص: ENABLE_DASHBOARD_AGENT=true.</p>
<textarea id="agent-request" rows="5" maxlength="4000" placeholder="اكتب طلب البحث أو المهمة للمساعد" dir="auto" style="width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #40536a;background:#10151d;color:#fff"></textarea>
<button onclick="runAgent()">تنفيذ طلب المساعد</button>
<pre id="agent-result" aria-live="polite"></pre></section>
<section class="card"><h2>إجراءات المهام المحدودة</h2>
<p class="muted">يجب إدخال رمز الدخول الظاهر في طرفية Codespaces. إضافة المهمة للطابور لا تشغّلها؛ التنفيذ إجراء منفصل.</p>

<select id="task-type" style="padding:10px;margin:8px"><option value="workspace_status">حالة مساحة العمل</option><option value="memory_recent">آخر الذكريات (قراءة فقط)</option></select>
<button onclick="enqueueTask()">إضافة للطابور</button><button onclick="runTask()">تنفيذ مهمة واحدة من الطابور</button>
<pre id="action-result" aria-live="polite"></pre></section>
<p class="muted">تغيير رمز الدخول يخفي النتائج الخاصة، لكنه لا يلغي طلبات أُرسلت بالفعل. افحص الحالة الحالية قبل إعادة إجراء لا تعرف نتيجته.</p>
<p class="muted">تتحدث المتابعة كل 30 ثانية. تسمح اللوحة بإضافة حالة مساحة العمل وآخر الذكريات فقط. لا تعرض أنواع مهام الخادم الأخرى، ولا تنفيذ shell أو كود عشوائي.</p>
<script>
let monitoringEpoch=0;
let monitoringRequest=0;
let reportRequest=0;
let actionRequest=0;
let agentRequest=0;
function clearPrivateData(){
 monitoringEpoch++;
 reportRequest++;
 actionRequest++;
 agentRequest++;
 document.getElementById('agent-result').textContent='';
 document.getElementById('action-result').textContent='';
 document.getElementById('saved-report').textContent='';
 document.getElementById('state').textContent='Locked';
 for(const id of ['runtime','memories','tasks','integrations','security','task-list','memory-list','plan-list']) document.getElementById(id).textContent='';
}
function showLoadError(){clearPrivateData();document.getElementById('state').textContent='Locked: check access token or connection';}
document.getElementById('control-token').addEventListener('input',clearPrivateData);
async function load(){
 const token=document.getElementById('control-token').value;
 if(!token){clearPrivateData();return}
 const epoch=monitoringEpoch;
 const request=++monitoringRequest;
 let data;
 try{data=await Promise.all(['/api/status','/api/tasks','/api/memories','/api/plans'].map(async path=>{
  const response=await fetch(path,{headers:{Authorization:'Bearer '+token}});
  if(!response.ok) throw new Error('Monitoring unavailable');
  return response.json();
 }));}catch(error){
  if(document.getElementById('control-token').value!==token || monitoringEpoch!==epoch || monitoringRequest!==request) return;
  throw error;
 }
 const [s,t,m,p]=data;
 if(document.getElementById('control-token').value!==token || monitoringEpoch!==epoch || monitoringRequest!==request) return;
 document.getElementById('state').textContent=s.status;
 document.getElementById('runtime').textContent='Python '+s.python+' · '+s.checked_at;
 document.getElementById('memories').textContent=s.memory_records??'Unavailable';
 document.getElementById('tasks').textContent=s.task_records??'Unavailable';
 document.getElementById('integrations').innerHTML=Object.entries(s.integrations).map(([k,v])=>'<li>'+k+': '+(v?'enabled':'disabled')+'</li>').join('');
 document.getElementById('security').textContent=JSON.stringify(s.security,null,2);
 document.getElementById('task-list').textContent=JSON.stringify(t,null,2);
 document.getElementById('memory-list').textContent=JSON.stringify(m,null,2);
 renderPlans(p);
}
function renderPlans(snapshot){
 const panel=document.getElementById('plan-list');
 if(!snapshot.available){panel.textContent='Plan store unavailable. No database created.';return}
 if(!snapshot.plans.length){panel.textContent='No plans yet.';return}
 const lines=[];
 for(const plan of snapshot.plans){
  lines.push('Plan '+plan.id+' · '+plan.status+' · '+plan.completed_steps+'/'+plan.total_steps+' completed');
  for(const step of plan.steps){
   lines.push('  '+step.position+'. '+step.task_type+' · '+step.status+' · execution '+(step.execution_status||'not bound')+' · attempts '+step.attempts);
   if(step.task_id) lines.push('     Task '+step.task_id);
   if(step.outcome_reconciliation_required) lines.push('     Waiting: outcome reconciliation required. No automatic replay.');
  }
  lines.push('');
 }
 if(snapshot.has_more) lines.push('More plans exist. Showing the latest bounded page.');
 panel.textContent=lines.join('\\n');
}
function clearSavedReport(){reportRequest++;document.getElementById('saved-report').textContent='';}
document.getElementById('report-plan').addEventListener('input',clearSavedReport);
document.getElementById('report-step').addEventListener('input',clearSavedReport);
async function loadSavedReport(){
 const output=document.getElementById('saved-report');
 const token=document.getElementById('control-token').value;
 const plan=document.getElementById('report-plan').value;
 const step=document.getElementById('report-step').value;
 const epoch=monitoringEpoch;const request=++reportRequest;
 if(!token){output.textContent='Enter the access token first.';return}
 const current=()=>document.getElementById('control-token').value===token && monitoringEpoch===epoch && reportRequest===request && document.getElementById('report-plan').value===plan && document.getElementById('report-step').value===step;
 output.textContent='Reading saved report...';
 try{
  const response=await fetch('/api/code-report?plan_id='+encodeURIComponent(plan)+'&step_id='+encodeURIComponent(step),{headers:{Authorization:'Bearer '+token}});
  if(!response.ok) throw new Error('Report unavailable');
  const result=await response.json();
  if(!current())return;
  const lines=['Saved quote-only report',result.question,''];
  for(const item of result.citations){
   lines.push(item.path+' · lines '+item.start_line+'-'+item.end_line);
   lines.push(item.quote);
   lines.push('File SHA256: '+item.file_sha256);
   lines.push('Excerpt SHA256: '+item.excerpt_sha256);
   lines.push('Untrusted source snapshot'+(item.truncated?' · excerpt only':'')+'\\n');
  }
  lines.push('Verification: '+result.verification);
  lines.push(...result.limitations);
  output.textContent=lines.join('\\n');
  if(document.getElementById('state').textContent.startsWith('Locked')){
   document.getElementById('state').textContent='Not loaded';
   document.getElementById('runtime').textContent='Saved report authenticated. Load monitoring separately.';
  }
 }catch(error){if(current())output.textContent='Saved report unavailable. Check IDs, token and completed state.';}
}
async function controlPost(path,body){
 const token=document.getElementById('control-token').value;
 const epoch=monitoringEpoch;const request=++actionRequest;
 const output=document.getElementById('action-result');
 if(!token){output.textContent='Enter the one-time token first.';return}
 const current=()=>document.getElementById('control-token').value===token && monitoringEpoch===epoch && actionRequest===request;
 try{
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(body)});
  const data=await response.json();
  if(!current())return;
  output.textContent=JSON.stringify(data,null,2);
  if(response.ok){
   try{await load();}
   catch(error){
    if(current()){
     document.getElementById('state').textContent='Monitoring unavailable';
     document.getElementById('runtime').textContent='Action response retained below. Monitoring refresh failed; do not repeat a successful action.';
    }
   }
  }
 }catch(error){if(current())output.textContent='Action result unavailable. Check current state before retrying; a submitted action may already have completed.';}
}
async function runAgent(){
 const request=document.getElementById('agent-request').value.trim();
 if(!request){document.getElementById('agent-result').textContent='Enter a request first.';return}
 if(!confirm('Send this request to the configured model? Model/API usage may incur charges or consume free-tier quota.')) return;
 const token=document.getElementById('control-token').value;
 if(!token){document.getElementById('agent-result').textContent='Enter the one-time token first.';return}
 const output=document.getElementById('agent-result');
 const epoch=monitoringEpoch;const currentRequest=++agentRequest;
 const current=()=>document.getElementById('control-token').value===token && monitoringEpoch===epoch && agentRequest===currentRequest;
 output.textContent='Agent is working…';
 try{
  const response=await fetch('/api/agent/run',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify({request})});
  const data=await response.json();
  if(current())output.textContent=JSON.stringify(data,null,2);
 }catch(error){if(current())output.textContent='Agent result unavailable. A submitted request may already have completed; check state before retrying.';}
}
async function enqueueTask(){
 if(!confirm('Enqueue this allowlisted task?')) return;
 const type=document.getElementById('task-type').value;
 const payload=type==='memory_recent'?{limit:5}:{};
 await controlPost('/api/tasks/enqueue',{task_type:type,payload,max_attempts:1});
}
async function runTask(){
 if(!confirm('Execute exactly one due task from the allowlist?')) return;
 await controlPost('/api/tasks/run-once',{});
}
clearPrivateData();
setInterval(()=>load().catch(showLoadError),30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AIWorkspaceControlPlane/1.1"

    def _trusted_host(self):
        hosts = self.headers.get_all("Host", [])
        allowed = getattr(self.server, "allowed_authorities", None)
        if allowed is None:
            allowed = allowed_authorities(self.server.server_port, {})
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            self._send(403, '{"error":"untrusted Host"}', "application/json")
            return False
        return True

    def _authorized(self):
        if len(self.headers.get_all("Authorization", [])) != 1:
            self._send(401, '{"error":"authentication required"}', "application/json")
            return False
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + getattr(self.server, "control_token", "")
        if not getattr(self.server, "control_token", "") or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            self._send(401, '{"error":"authentication required"}', "application/json")
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin:
            try:
                parsed = urlparse(origin)
            except ValueError:
                self._send(403, '{"error":"invalid Origin"}', "application/json")
                return False
            if (parsed.netloc.lower() != host.lower() or parsed.scheme not in {"http", "https"}
                    or parsed.path or parsed.query or parsed.fragment or len(self.headers.get_all("Origin", [])) != 1):
                self._send(403, '{"error":"cross-origin request rejected"}', "application/json")
                return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site != "same-origin":
            self._send(403, '{"error":"cross-site request rejected"}', "application/json")
            return False
        return True

    def _read_json(self, *, unique_keys=False):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit() or not 1 <= int(raw_length) <= 8192:
            raise ValueError("Request body must be between 1 and 8192 bytes")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result
        value = json.loads(self.rfile.read(int(raw_length)), object_pairs_hook=pairs if unique_keys else dict)
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
        if not self._trusted_host():
            return
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, _page(), "text/html; charset=utf-8")
        if path.startswith("/api/") and not self._authorized():
            return
        if path == "/api/code-report":
            try:
                from agent.code_report_cli import read_saved_report
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                if set(query) != {"plan_id", "step_id"} or any(len(value) != 1 for value in query.values()):
                    raise ValueError("unsupported_query")
                database = os.environ.get("WORKSPACE_PLAN_DB", ".data/plans.sqlite3")
                result = read_saved_report(database, query["plan_id"][0], query["step_id"][0])
                return self._send(200, json.dumps(result, ensure_ascii=True), "application/json")
            except (ValueError, sqlite3.Error, OSError):
                return self._send(404, '{"error":"saved code report unavailable"}', "application/json")
        if path == "/api/plans":
            try:
                from agent.plan_monitoring import plan_snapshot
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                if set(query) - {"limit"} or any(len(value) != 1 for value in query.values()):
                    raise ValueError("unsupported_query")
                limit = int(query["limit"][0]) if "limit" in query else 20
                if not 1 <= limit <= 50:
                    raise ValueError("invalid_limit")
                return self._send(200, json.dumps(plan_snapshot(limit)), "application/json")
            except ValueError:
                return self._send(400, '{"error":"invalid plan query or store"}', "application/json")
            except (sqlite3.Error, OSError):
                return self._send(503, '{"error":"plan monitoring unavailable"}', "application/json")
        if path == "/api/status":
            return self._send(200, json.dumps(status_snapshot()), "application/json")
        if path == "/api/tasks":
            return self._send(200, json.dumps(task_snapshot(), default=str), "application/json")
        if path == "/api/adult/tasks" or path.startswith("/api/adult/tasks/"):
            return self._adult_get(path)
        if path == "/api/memories":
            return self._send(200, json.dumps(memory_snapshot(), default=str), "application/json")
        return self._send(404, '{"error":"not found"}', "application/json")

    def _adult_get(self, path):
        from agent.adult.models import AdultModelValidationError, TASK_STATUSES
        from agent.adult.adult_task_store import AdultTaskStoreError
        try:
            if path == "/api/adult/tasks":
                query = parse_qs(urlparse(self.path).query)
                if set(query) - {"limit", "status"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("unsupported query")
                limit = int(query["limit"][0]) if "limit" in query else 20
                status = query["status"][0] if "status" in query else None
                if not 1 <= limit <= 50 or (status is not None and status not in TASK_STATUSES):
                    raise ValueError("invalid query")
                return self._send(200, json.dumps(adult_tasks_snapshot(limit, status)), "application/json")
            task_id = path[len("/api/adult/tasks/"):]
            if urlparse(self.path).query or not _ADULT_TASK_ID_RE.fullmatch(task_id):
                raise ValueError("invalid task id")
            detail = adult_task_detail(task_id)
            if detail is None:
                return self._send(404, '{"error":"not found"}', "application/json")
            return self._send(200, json.dumps(detail), "application/json")
        except (ValueError, AdultModelValidationError):
            return self._send(400, '{"error":"invalid request"}', "application/json")
        except (AdultTaskStoreError, sqlite3.Error, OSError):
            return self._send(503, '{"error":"Adult status unavailable"}', "application/json")

    def _adult_decision(self, path):
        """Local control-token holder cancellation only, never owner identity proof."""
        if path != "/api/adult/cancel":
            return self._send(503, '{"error":"Adult execution disabled"}', "application/json")
        from agent.adult.adult_task_store import (AdultTaskStore, StaleTaskVersion,
            InvalidTaskTransition, TaskTerminal, TaskNotFound, AdultTaskStoreError)
        try:
            body = self._read_json(unique_keys=True)
            if set(body) != {"task_id", "expected_state_version", "decision_id"}:
                raise ValueError("invalid decision fields")
            if (type(body["task_id"]) is not str or not _ADULT_TASK_ID_RE.fullmatch(body["task_id"])
                    or type(body["expected_state_version"]) is not int or body["expected_state_version"] < 0
                    or type(body["decision_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}",body["decision_id"])):
                raise ValueError("invalid decision")
            readonly = _adult_store()
            if readonly is None:
                return self._send(404, '{"error":"task unavailable"}', "application/json")
            # Read the same configured path. No body field can choose an owner,
            # path, application, model, endpoint or execution permission.
            tasks = AdultTaskStore(readonly._db_path)
            task = tasks.request_cancel(body["task_id"], expected_state_version=body["expected_state_version"],
                                        decision_id=body["decision_id"])
            cleanup = "deferred_to_worker"
            if task.status == "cancelled":
                try:
                    from agent.adult.payload_store import AdultArtifactStore
                    artifacts = getattr(self.server, "adult_artifacts", None)
                    if artifacts is None:
                        root = Path(os.environ.get("ADULT_PAYLOAD_DIR") or ".data/adult_payloads")
                        if root.is_dir():
                            artifacts = AdultArtifactStore.from_environment()
                    if artifacts is None:
                        cleanup = "no_input_store"
                    elif isinstance(artifacts, AdultArtifactStore):
                        artifacts.delete_task_inputs(task.task_id)
                        cleanup = "input_removed"
                    else:
                        cleanup = "cleanup_failed"
                except Exception:
                    cleanup = "cleanup_failed"
            return self._send(200, json.dumps({"task":_adult_task_view(task), "input_cleanup":cleanup}), "application/json")
        except TaskNotFound:
            return self._send(404, '{"error":"task unavailable"}', "application/json")
        except (StaleTaskVersion, InvalidTaskTransition, TaskTerminal):
            return self._send(409, '{"error":"decision conflict"}', "application/json")
        except ValueError:
            return self._send(400, '{"error":"invalid decision"}', "application/json")
        except (AdultTaskStoreError, sqlite3.Error, OSError):
            return self._send(503, '{"error":"Adult decision unavailable"}', "application/json")

    def do_POST(self):
        if not self._trusted_host():
            return
        path = urlparse(self.path).path
        if path in {"/api/adult/cancel", "/api/adult/continue", "/api/adult/run"}:
            if not self._authorized():
                return
            with self.server.action_lock:
                now = time.monotonic()
                self.server.action_times = [stamp for stamp in self.server.action_times if now - stamp < 60]
                if len(self.server.action_times) >= 10:
                    return self._send(429, '{"error":"action rate limit exceeded"}', "application/json")
                self.server.action_times.append(now)
            return self._adult_decision(path)
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
                if not self.server.agent_lock.acquire(blocking=False):
                    return self._send(429, '{"error":"another Agent request is already running"}', "application/json")
                try:
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
                finally:
                    self.server.agent_lock.release()
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
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the authenticated AI workspace control dashboard.")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Port must be between 1024 and 65535.")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.allowed_authorities = allowed_authorities(server.server_port)
    server.control_token = secrets.token_urlsafe(32)
    server.action_lock = threading.Lock()
    server.action_times = []
    server.agent_lock = threading.Lock()
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
