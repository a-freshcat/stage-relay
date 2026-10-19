"""Run the actual result/issue helpers; no model calls or browser emulation."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ResultExplanationTests(unittest.TestCase):
    def test_saved_answers_duplicate_notices_and_review_findings(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
function part(start,end){return source.slice(source.indexOf(start),source.indexOf(end));}
const ctx={strings:v=>Array.isArray(v)?v:[],checkPassed:c=>c.exit_code===0&&!c.failure,
 workerResponse:d=>({payload:d.result}),currentPhase:()=>null};
vm.createContext(ctx);
vm.runInContext(part('function workerAnswer(','function helpButton(')+part('function issue(','function renderIssues('),ctx);
const old={status:'completed',reason:'接力验证成功，OK。',residual:[
 '未配置任何验收检查（configured checks 为空），因此无检查可运行。','calc.py 中原有错误按方案保留。']};
assert.equal(ctx.workerAnswer(old),'接力验证成功，OK。');
assert.equal(ctx.workerAnswer({...old,answer:'真实回答'}),'真实回答');
assert.equal(ctx.workerAnswer({status:'blocked',reason:'不是回答'}),'');
const data={result:old,checks:[],phases:[],state:'needs_manual_verification'};
let issues=ctx.issuesFor(data);
assert.equal(issues.length,1);assert.equal(issues[0].label,'待核对的提醒');
assert.equal(old.residual.length,2); // original report is retained in history
data.review={status:'changes_requested',summary:'目标未完成',evidence:['依据'],issues:['修复指定文件']};
issues=ctx.issuesFor(data);assert.equal(issues.length,2);assert.equal(issues[1].tone,'bad');
data.review.stale=true;assert.equal(ctx.issuesFor(data).length,1);
data.review={status:'approved',resolved_residual:[1],evidence:['已核对，属于原方案明确保留的范围']};
assert.equal(ctx.issuesFor(data).length,0);
data.review.stale=true;assert.equal(ctx.issuesFor(data).length,1);
console.log('result helpers verified');
'''
        result = subprocess.run([shutil.which('node') or 'node', '-e', script, str(ROOT/'panel/panel.js')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_model_test_has_plain_name_and_own_description(self):
        html = (ROOT/'panel/index.html').read_text(encoding='utf-8')
        for name, identity in [('查看模型列表', 'catalog-help'), ('试着回答一句', 'generation-help'),
                               ('试跑接力格式', 'compat-help')]:
            self.assertIn(name, html)
            self.assertIn('aria-describedby="'+identity+'"', html)
            self.assertIn('id="'+identity+'"', html)
        self.assertIn('id="issue-help-dialog"', html)
        self.assertIn('id="help-worker"', html)
        self.assertIn('id="help-checks"', html)


if __name__ == '__main__':
    unittest.main()
