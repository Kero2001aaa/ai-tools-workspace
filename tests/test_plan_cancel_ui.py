"""Actual JS cancellation preview, single submit and privacy fences."""
import re
import shutil
import subprocess
import unittest
from agent.control_plane import _page


@unittest.skipUnless(shutil.which('node'),'Node unavailable')
class PlanCancelUITests(unittest.TestCase):
    def test_explicit_preview_snapshot_single_submit_token_and_selection_fences(self):
        script=re.search(r'<script>(.*?)</script>',_page(),re.S).group(1)
        harness='''
const assert=require('assert');const nodes=new Map();
function node(){return {value:'',textContent:'',disabled:false,children:[],listeners:{},appendChild(x){this.children.push(x)},addEventListener(name,fn){this.listeners[name]=fn},set innerHTML(x){throw Error('unsafe HTML')}}}
global.document={createElement:node,getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)}};
global.setInterval=()=>0;let posts=[];let pending=[];let previews=[];let consent=true;
global.confirm=(text)=>{previews.push(text);return consent};
global.fetch=(path,options)=>{posts.push({path,options});return new Promise((resolve,reject)=>pending.push({resolve,reject}))};
const plans={available:true,plans:[{id:'plan-id',updated_at:'version-one',status:'checkpointed',completed_steps:1,total_steps:2,steps:[{status:'succeeded'},{status:'pending'}]},{id:'running',updated_at:'two',status:'running',steps:[]}]};
function select(){document.getElementById('control-token').value='token';renderPlans(plans);document.getElementById('cancel-plan').value='plan-id';}
'''
        checks='''
(async()=>{
 select();assert.equal(cancellablePlans.size,1);
 consent=false;await cancelSelectedPlan();assert.equal(posts.length,0);assert.match(previews[0],/plan-id/);assert.match(previews[0],/1\/2/);
 consent=true;let first=cancelSelectedPlan();await cancelSelectedPlan();assert.equal(posts.length,1);assert.equal(document.getElementById('cancel-plan-button').disabled,true);
 renderPlans({available:true,plans:[]});assert.equal(cancellablePlans.size,1);
 assert.equal(posts[0].path,'/api/plans/cancel');assert.deepEqual(JSON.parse(posts[0].options.body),{plan_id:'plan-id',expected_updated_at:'version-one'});
 const epochBefore=monitoringRequest;pending.shift().resolve({ok:true,json:async()=>({status:'cancelled'})});await first;assert.ok(monitoringRequest>epochBefore);assert.match(document.getElementById('plan-cancel-result').textContent,/تم إلغاء/);assert.equal(cancellablePlans.size,0);await cancelSelectedPlan();assert.equal(posts.length,1);
 select();first=cancelSelectedPlan();clearPrivateData();pending.shift().resolve({ok:true,json:async()=>({status:'cancelled'})});await first;assert.equal(document.getElementById('plan-cancel-result').textContent,'');
 select();first=cancelSelectedPlan();document.getElementById('cancel-plan').value='other';document.getElementById('cancel-plan').listeners.change();pending.shift().resolve({ok:false,json:async()=>({error:'private'})});await first;assert.equal(document.getElementById('plan-cancel-result').textContent,'');
 select();first=cancelSelectedPlan();pending.shift().reject(Error('lost'));await first;assert.match(document.getElementById('plan-cancel-result').textContent,/غير معروفة/);assert.equal(cancellablePlans.size,0);
 const count=posts.length;await cancelSelectedPlan();assert.equal(posts.length,count);
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result=subprocess.run(['node','-e',harness+script+checks],capture_output=True,text=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stderr)
