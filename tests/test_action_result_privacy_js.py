"""Execute production action JS with token change and out-of-order responses."""
import re
import shutil
import subprocess
import unittest
from agent.control_plane import _page


@unittest.skipUnless(shutil.which('node'),'Node unavailable')
class ActionPrivacyJSTests(unittest.TestCase):
    def test_late_success_failure_and_reentered_token_cannot_repopulate(self):
        script=re.search(r'<script>(.*?)</script>',_page(),re.S).group(1)
        harness='''
const assert=require('assert');const nodes=new Map();
global.document={getElementById(id){if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',addEventListener(){}});return nodes.get(id)}};
global.setInterval=()=>0;global.confirm=()=>true;
const pending=[];global.fetch=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
'''
        checks='''
(async()=>{
 for(const run of [()=>controlPost('/api/tasks/run-once',{}),()=>runAgent()]){
  const token=document.getElementById('control-token');token.value='old';document.getElementById('agent-request').value='synthetic';
  let work=run();token.value='new';clearPrivateData();pending.shift().resolve({ok:false,json:async()=>({secret:'late'})});await work;
  assert.equal('',document.getElementById('agent-result').textContent);assert.equal('',document.getElementById('action-result').textContent);
  token.value='old';work=run();clearPrivateData();pending.shift().reject(new Error('private error'));await work;
  assert.equal('',document.getElementById('agent-result').textContent);assert.equal('',document.getElementById('action-result').textContent);
  token.value='old';const first=run();clearPrivateData();token.value='old';const second=run();const stale=pending.shift();pending.shift().resolve({ok:false,json:async()=>({marker:'newest'})});await second;
  stale.resolve({ok:false,json:async()=>({marker:'stale'})});await first;
  const output=document.getElementById('agent-result').textContent+document.getElementById('action-result').textContent;
  assert.match(output,/newest/);assert.doesNotMatch(output,/stale/);clearPrivateData();
  const older=run();const newer=run();const olderResponse=pending.shift();pending.shift().resolve({ok:false,json:async()=>({marker:'latest same token'})});await newer;olderResponse.reject(new Error('older'));await older;
  const sameToken=document.getElementById('agent-result').textContent+document.getElementById('action-result').textContent;assert.match(sameToken,/latest same token/);assert.doesNotMatch(sameToken,/older/);clearPrivateData();
 }
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result=subprocess.run(['node','-e',harness+script+checks],capture_output=True,text=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stderr)
