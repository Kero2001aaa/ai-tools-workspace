"""Execute actual saved-report UI script with adversarial late responses."""
import re
import shutil
import subprocess
import unittest
from agent.control_plane import _page


@unittest.skipUnless(shutil.which('node'), 'Node unavailable')
class SavedReportJSTests(unittest.TestCase):
    def test_safe_literal_render_and_token_selection_latest_request_fences(self):
        script=re.search(r'<script>(.*?)</script>',_page(),re.S).group(1)
        harness='''
const assert=require('assert');const nodes=new Map();
global.document={getElementById(id){if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',addEventListener(){},set innerHTML(v){throw Error('unsafe render')}});return nodes.get(id)}};
global.setInterval=()=>0;
const pending=[];global.fetch=()=>new Promise(resolve=>pending.push(resolve));
const sample={question:'<script>literal</script>',citations:[{path:'source.py',start_line:1,end_line:2,quote:'<img src=x>',file_sha256:'a'.repeat(64),excerpt_sha256:'b'.repeat(64),truncated:true}],verification:'snapshot only',limitations:['not model understanding']};
const ok=()=>({ok:true,json:async()=>sample});
'''
        checks='''
(async()=>{
 document.getElementById('control-token').value='token';document.getElementById('report-plan').value='plan';document.getElementById('report-step').value='step';
 document.getElementById('state').textContent='Locked';
 let call=loadSavedReport();pending.shift()(ok());await call;assert.match(document.getElementById('saved-report').textContent,/<img src=x>/);assert.equal('Not loaded',document.getElementById('state').textContent);assert.match(document.getElementById('runtime').textContent,/Saved report authenticated/);
 call=loadSavedReport();clearPrivateData();pending.shift()(ok());await call;assert.equal(document.getElementById('saved-report').textContent,'');
 const old=loadSavedReport();document.getElementById('report-step').value='new';clearSavedReport();const recent=loadSavedReport();
 const oldResolve=pending.shift();pending.shift()(ok());await recent;const text=document.getElementById('saved-report').textContent;
 oldResolve({ok:false});await old;assert.equal(text,document.getElementById('saved-report').textContent);
 clearSavedReport();assert.equal('',document.getElementById('saved-report').textContent);
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result=subprocess.run(['node','-e',harness+script+checks],capture_output=True,text=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stderr)
