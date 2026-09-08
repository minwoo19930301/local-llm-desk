"""Bounded, redacted tool evidence without models, requests, files, or alerts."""
from contextlib import ExitStack, nullcontext
import json
import unittest
from unittest import mock

from desk import connectors, runner, tools


class ToolEvidence(unittest.TestCase):
    def execute(self, replies, run_tool):
        ctx = runner.RunContext(job_id="fixture", title="fixture", model="stub", prompt="fixture", tools=["http"], connector_ids=["fixture"], max_loops=1)
        cons = [{"id": "fixture", "kind": "http", "name": "fixture"}]
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(runner.ollama_ctl, "session", return_value=nullcontext()))
            stack.enter_context(mock.patch.object(runner.ollama_ctl, "chat_messages", side_effect=replies))
            stack.enter_context(mock.patch.object(runner, "_collect_connectors", return_value=cons))
            stack.enter_context(mock.patch.object(connectors, "resolve_env", return_value={"TOKEN": "SENSITIVE_SECRET"}))
            stack.enter_context(mock.patch.object(connectors, "resolve_headers", return_value={}))
            stack.enter_context(mock.patch.object(tools, "build_context", return_value=tools.ToolContext("read", {})))
            stack.enter_context(mock.patch.object(tools, "run", side_effect=run_tool))
            return runner._execute(ctx, {"action": "run", "model": "stub"}, 0)

    def call(self):
        return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "http__fixture", "arguments": {}}}]}}

    def final(self):
        return {"message": {"role": "assistant", "content": "summary"}}

    def test_success_keeps_tool_output_separate_from_model_summary(self):
        result = self.execute([self.call(), self.final()], lambda *args, **kwargs: '{"title":"source title","url":"https://example.invalid/post"}')
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output"], "summary")
        row = result["tool_results"][0]
        self.assertEqual(row["name"], "http__fixture")
        self.assertTrue(row["ok"])
        self.assertIn("source title", row["output"])
        self.assertFalse(result["tool_results_truncated"])

    def test_typed_tool_failure_is_recorded_and_scrubbed(self):
        def failed(name, *args, context, **kwargs):
            context.failures.append(tools.ToolFailure(name, "HTTPError", "HTTP 503 SENSITIVE_SECRET"))
            return "도구 실패: HTTP 503 SENSITIVE_SECRET"
        result = self.execute([self.call()], failed)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["tool_results"][0]["error_type"], "HTTPError")
        self.assertFalse(result["tool_results"][0]["ok"])
        self.assertIn("HTTP 503", result["tool_results"][0]["output"])
        self.assertNotIn("SENSITIVE_SECRET", json.dumps(result))
        self.assertIn("[REDACTED]", result["tool_results"][0]["output"])

    def test_model_failure_after_fetch_preserves_fetched_evidence(self):
        result = self.execute([self.call(), TimeoutError("summary timed out")], lambda *args, **kwargs: "fetched evidence")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["tool_results"][0]["output"], "fetched evidence")
        self.assertTrue(result["tool_results"][0]["ok"])
        self.assertIn("summary timed out", result["error"])

    def test_unexpected_tool_exception_is_also_recorded(self):
        result = self.execute([self.call()], RuntimeError("unexpected tool error"))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["tool_results"][0]["error_type"], "RuntimeError")
        self.assertFalse(result["tool_results"][0]["ok"])

    def test_secrets_are_removed_before_output_is_truncated(self):
        raw = "x" * (runner.TOOL_RESULT_MAX_CHARS - 5) + "SENSITIVE_SECRET" + "tail" * 2000
        result = self.execute([self.call(), self.final()], lambda *args, **kwargs: raw)
        row = result["tool_results"][0]
        self.assertLessEqual(len(row["output"]), runner.TOOL_RESULT_MAX_CHARS)
        self.assertNotIn("SENSI", row["output"])
        self.assertTrue(row["truncated"])
        self.assertTrue(result["tool_results_truncated"])

    def test_total_budget_and_count_are_bounded_and_terminal_failure_retained(self):
        task = runner.TaskResult()
        for index in range(100):
            task.note_tool_result("fetch", "x" * 10000, ok=True)
        task.note_tool_result("final_failure", "failed", ok=False, error_type="HTTPError")
        result = runner._record(runner.RunContext(job_id="fixture", title="fixture", model="stub", prompt="fixture"), "fail", task=task)
        self.assertLessEqual(len(result["tool_results"]), runner.TOOL_RESULTS_MAX_ENTRIES)
        self.assertLessEqual(sum(len(row["output"]) for row in result["tool_results"]), runner.TOOL_RESULTS_TOTAL_CHARS)
        self.assertTrue(result["tool_results_truncated"])
        self.assertGreater(result["tool_results_omitted"], 0)
        self.assertFalse(result["tool_results"][-1]["ok"])
        self.assertEqual(result["tool_results"][-1]["error_type"], "HTTPError")
        self.assertEqual(result["tool_results"][-1]["output"], "failed")
        self.assertLess(len(json.dumps(result)), 55000)


if __name__ == "__main__":
    unittest.main()
