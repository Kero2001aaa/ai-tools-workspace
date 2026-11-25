"""Execute dashboard JS against a minimal DOM, catching syntax and stale-data bugs.

Node is optional for normal Python installs; when present this executes production
script source, rather than merely asserting substrings in generated HTML.
"""
import json
import re
import shutil
import subprocess
import unittest
from agent.control_plane import _page


@unittest.skipUnless(shutil.which('node'), 'Node unavailable for optional DOM regression')
class PlanDashboardJSTests(unittest.TestCase):
    def test_render_empty_unavailable_escaping_and_token_clear(self):
        script = re.search(r'<script>(.*?)</script>', _page(), re.S).group(1)
        harness = '''
const assert=require('assert');
const elements=new Map();
global.document={createElement(){return {value:"",textContent:""}},getElementById(id){
 if(!elements.has(id))elements.set(id,{textContent:'',value:'',appendChild(){},addEventListener(){},set innerHTML(x){throw Error('unsafe HTML')}});
 return elements.get(id);
}};
global.setInterval=()=>0;
'''
        checks = '''
renderPlans({available:false});assert.match(document.getElementById('plan-list').textContent,/unavailable/);
renderPlans({available:true,plans:[]});assert.equal(document.getElementById('plan-list').textContent,'No plans yet.');
renderPlans({available:true,has_more:true,plans:[{id:'<img src=x>',status:'running',completed_steps:0,total_steps:1,steps:[{position:1,task_type:'workspace_status',status:'running',execution_status:'running',attempts:1,task_id:'task-id',outcome_reconciliation_required:true}]}]});
const output=document.getElementById('plan-list').textContent;
assert.match(output,/<img src=x>/);assert.match(output,/No automatic replay/);assert.match(output,/More plans/);
clearPrivateData();assert.equal(document.getElementById('plan-list').textContent,'');
console.log('DOM states, safe text rendering and token-clear passed');
'''
        result = subprocess.run(['node', '-e', harness + script + checks], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_stale_fetch_response_cannot_repopulate_after_token_change(self):
        script = re.search(r'<script>(.*?)</script>', _page(), re.S).group(1)
        harness = '''
const assert=require('assert');const elements=new Map();
global.document={createElement(){return {value:"",textContent:""}},getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',value:'',appendChild(){},addEventListener(){}});return elements.get(id);}};
global.setInterval=()=>0;let releases=[];
global.fetch=()=>new Promise(resolve=>releases.push(()=>resolve({ok:true,json:async()=>({})})));
'''
        checks = '''
(async()=>{document.getElementById('control-token').value='old';const pending=load();
document.getElementById('control-token').value='new';clearPrivateData();
releases.forEach(release=>release());await pending;
assert.equal(document.getElementById('plan-list').textContent,'');
console.log('Late response after token change ignored');})().catch(e=>{console.error(e);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', harness + script + checks], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_old_response_after_clear_and_same_token_reentry_is_ignored(self):
        script = re.search(r'<script>(.*?)</script>', _page(), re.S).group(1)
        harness = """
const assert=require('assert');const elements=new Map();
global.document={createElement(){return {value:"",textContent:""}},getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',value:'',appendChild(){},addEventListener(){}});return elements.get(id);}};
global.setInterval=()=>0;let releases=[];
global.fetch=()=>new Promise(resolve=>releases.push(()=>resolve({ok:true,json:async()=>({})})));
"""
        checks = """
(async()=>{document.getElementById('control-token').value='same';const pending=load();
clearPrivateData();document.getElementById('control-token').value='same';
releases.forEach(release=>release());await pending;
assert.equal(document.getElementById('plan-list').textContent,'');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run(['node', '-e', harness + script + checks], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_newer_load_wins_even_with_same_token(self):
        script = re.search(r'<script>(.*?)</script>', _page(), re.S).group(1)
        harness = """
const assert=require('assert');const elements=new Map();
global.document={createElement(){return {value:"",textContent:""}},getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',value:'',appendChild(){},addEventListener(){},innerHTML:''});return elements.get(id);}};
global.setInterval=()=>0;let releases=[];
global.fetch=(path)=>new Promise(resolve=>releases.push((label)=>resolve({ok:true,json:async()=>path==='/api/status'?{status:label,integrations:{},security:{}}:path==='/api/plans'?{available:false}:[]})));
"""
        checks = """
(async()=>{document.getElementById('control-token').value='same';const old=load();const fresh=load();
releases.slice(4).forEach(release=>release('new'));await fresh;
releases.slice(0,4).forEach(release=>release('old'));await old;
assert.equal(document.getElementById('state').textContent,'new');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run(['node', '-e', harness + script + checks], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_old_failed_request_cannot_lock_newer_success(self):
        script = re.search(r'<script>(.*?)</script>', _page(), re.S).group(1)
        harness = """
const assert=require('assert');const elements=new Map();
global.document={createElement(){return {value:"",textContent:""}},getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',value:'',appendChild(){},addEventListener(){},innerHTML:''});return elements.get(id);}};
global.setInterval=()=>0;let releases=[];
global.fetch=(path)=>new Promise((resolve,reject)=>releases.push({reject,resolve:()=>resolve({ok:true,json:async()=>path==='/api/status'?{status:'new',integrations:{},security:{}}:path==='/api/plans'?{available:false}:[]})}));
"""
        checks = """
(async()=>{document.getElementById('control-token').value='same';const old=load().catch(showLoadError);const fresh=load().catch(showLoadError);
releases.slice(4).forEach(item=>item.resolve());await fresh;
releases.slice(0,4).forEach(item=>item.reject(Error('old failure')));await old;
assert.equal(document.getElementById('state').textContent,'new');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run(['node', '-e', harness + script + checks], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
