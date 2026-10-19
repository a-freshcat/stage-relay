"""Panel summary layout checks: static assets and record-only rendering, no models."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "panel" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "panel" / "panel.js").read_text(encoding="utf-8")


class SummaryMarkupTests(unittest.TestCase):
    def test_summary_ids_and_history_collapsed_by_default(self):
        for name in ("request", "result", "issues", "issue-count", "progress", "progress-stage", "history"):
            self.assertIn('id="' + name + '"', HTML)
        history = re.search(r'<details id="history"[^>]*>', HTML)
        self.assertIsNotNone(history)
        self.assertNotIn("open", history.group(0))
        self.assertLess(HTML.index('id="result"'), HTML.index('id="planner"'))

    def test_assets_never_call_models(self):
        # Viewing and refreshing only read local records; models start from the
        # explicit start-task form, never from rendering or polling.
        self.assertNotIn("api/models", JS)
        self.assertNotIn("https://", JS)
        poll = JS[JS.index("async function refresh("):JS.index('$("run").addEventListener("change"')]
        self.assertNotIn("api/jobs", poll)
        self.assertIn("api/run/", poll)

    def test_field_meaning_is_not_borrowed(self):
        readable = JS[JS.index("function readable("):JS.index("function reportText")]
        self.assertIn('value.status==="needs_replan"', readable)
        self.assertIn("调整原因", readable)
        self.assertIn("阻塞原因", readable)
        self.assertIn("决策依据", readable)
        # A completed result must not show a generic reason, and the hook that
        # renders replan/blocked text is gated by the result status.
        self.assertNotIn('line("原因"', JS)
        self.assertNotIn("原因：", readable.split('if(value.status==="needs_replan")')[0])
        self.assertIn("options.skipStatus", readable)

    def test_result_distinguishes_self_report_and_independent_checks(self):
        self.assertIn("执行端报告完成", JS)
        self.assertIn("独立检查", JS)
        self.assertIn("待人工验证", JS)

    def test_handoffs_collapse_steps_and_keep_raw_json(self):
        self.assertIn("handoff-details", JS)
        self.assertIn("交接目的", JS)
        self.assertIn("原始交接包", JS)
        self.assertIn("stepsInDetails", JS)

    def test_generation_checks_warn_about_possible_billing(self):
        # Both the static note and the pre-click confirmation must mention cost.
        self.assertIn("可能产生费用", HTML)
        self.assertIn("可能产生费用", JS)
        confirm = JS[JS.index("async function runCheck("):JS.index('$("check-catalog")')]
        self.assertIn("window.confirm", confirm)
        self.assertIn("kind!==\"catalog\"", confirm)
        # Cancelling must not send the request.
        self.assertIn("已取消", confirm)
        # Original-planner review has a separate billing confirmation too.

    def test_check_records_show_stage_and_http_status(self):
        block = JS[JS.index("function renderChecks("):JS.index("async function loadChecks(")]
        self.assertIn("HTTP ", block)
        self.assertIn("失败/完成阶段", block)
        self.assertIn("record.http_status", block)


if __name__ == "__main__":
    unittest.main()
