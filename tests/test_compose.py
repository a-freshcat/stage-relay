"""Exercise the actual browser composer with Node; no models or DOM mock."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class ComposeTests(unittest.TestCase):
    def test_actual_composer_preserves_plan_and_transmits_supplement(self):
        script = r'''
const assert=require('node:assert/strict');
const compose=require(process.argv[1]);
const draft={project:'C:\\sample',plan:{goal:'修复页面',files:[{path:'a.js',note:'事实'}],steps:['修复'],constraints:['保留配置']}};
const original=JSON.stringify(draft);
const plan=compose.callerPlan(draft,'c:/sample/','修复页面','新增中文说明');
assert.deepEqual(plan.constraints,['保留配置','新增中文说明']);
assert.equal(JSON.stringify(draft),original);
assert.deepEqual(compose.withSupplement(plan,'新增中文说明'),plan);
const withOriginal={...draft,user_request:'请修复页面，并保持现有操作方式'};
assert.equal(compose.callerPlan(withOriginal,'C:/sample',withOriginal.user_request,'').goal,'修复页面');
assert.throws(()=>compose.callerPlan(draft,'c:/sample','完全不同的新任务',''),/任务指示已更改/);
assert.throws(()=>compose.callerPlan(draft,'c:/other','修复页面',''),/项目已更改/);
assert.throws(()=>compose.callerPlan(null,'c:/sample','新任务',''),/准备方案/);
const text=compose.handoff('C:/sample','新任务','保留配置');
assert.ok(text.includes('新任务')&&text.includes('保留配置')&&text.includes('relay_prepare'));
const same=compose.syncDraft(withOriginal,{...withOriginal,id:'new-plan'},'新增要求');
assert.equal(same.supplement,'新增要求');
const next=compose.syncDraft(withOriginal,{project:'D:/new-project',user_request:'回答 OK',plan:{goal:'回答 OK'}},'上个任务的补充');
assert.equal(next.project,'D:/new-project');assert.equal(next.request,'回答 OK');assert.equal(next.supplement,'');
const request=compose.executionRequest('原始完整任务','改为中文');
assert.ok(request.startsWith('原始完整任务')&&request.includes('改为中文')&&request.includes('以本次补充为准'));
console.log('compose verified');
'''
        result = subprocess.run([shutil.which('node') or 'node', '-e', script,
                                 str(ROOT/'panel/compose.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inputs_are_visible_and_page_serves_composer_before_ui(self):
        html = (ROOT/'panel/index.html').read_text(encoding='utf-8')
        self.assertIn('id="launch-extra"', html)
        self.assertIn('给当前 Codex 的任务指示', html)
        self.assertLess(html.index('src="compose.js"'), html.index('src="panel.js"'))
        self.assertEqual(html.count('type="submit"'), 1)
        self.assertNotIn('id="draft-refresh"', html)
        self.assertNotIn('id="caller-start"', html)
