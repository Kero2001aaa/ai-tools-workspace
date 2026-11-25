"""Successful action response survives failed subsequent monitoring refresh."""
import re
import shutil
import subprocess
import unittest
from agent.control_plane import _page


@unittest.skipUnless(shutil.which('node'),'Node unavailable')
class ActionFollowupJSTests(unittest.TestCase):
    def test_success_retained_and_monitoring_problem_separate_without_retry(self):
        script=re.search(r'<script>(.*?)</script>',_page(),re.S).group(1)
        harness='''
const assert=require('assert');const nodes=new Map();let posts=0;
global.document={getElementById(id){if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',addEventListener(){}});return nodes.get(id)}};
global.setInterval=()=>0;global.fetch=async(path,options)=>{
 if(options.method==='POST'){posts++;return {ok:true,json:async()=>({status:'succeeded',task_id:'original'})}}
 throw new Error('monitoring disconnected');
};
'''
        checks='''
(async()=>{document.getElementById('control-token').value='synthetic';
 await controlPost('/api/tasks/run-once',{});
 assert.match(document.getElementById('action-result').textContent,/succeeded/);
 assert.match(document.getElementById('action-result').textContent,/original/);
 assert.equal('Monitoring unavailable',document.getElementById('state').textContent);
 assert.match(document.getElementById('runtime').textContent,/Action response retained/);
 assert.equal(1,posts);
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result=subprocess.run(['node','-e',harness+script+checks],capture_output=True,text=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stderr)
