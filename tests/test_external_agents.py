import json
import subprocess
import unittest
from unittest import mock

from desk import runner, tools


class LocalAgentSelection(unittest.TestCase):
    def test_opencode_uses_selected_local_model_and_no_cloud_provider(self):
        with mock.patch.object(runner, "_run_cli", return_value="ok") as execute:
            runner.run_task("qwen3:8b", "fixture", agent="opencode")
        args, kwargs = execute.call_args
        self.assertEqual(args[1], ["opencode", "run", "--model", "ollama/qwen3:8b", "fixture"])
        config = json.loads(kwargs["extra_env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["enabled_providers"], ["ollama"])
        self.assertEqual(config["small_model"], "ollama/qwen3:8b")
        self.assertEqual(config["provider"]["ollama"]["options"]["baseURL"], "http://127.0.0.1:11434/v1")

    def test_aider_gets_local_model_and_base_url(self):
        with mock.patch.object(runner, "_run_cli", return_value="ok") as execute:
            runner.run_task("qwen3:8b", "fixture", agent="aider")
        self.assertIn("ollama_chat/qwen3:8b", execute.call_args.args[1])
        self.assertEqual(execute.call_args.kwargs["extra_env"], {"OLLAMA_API_BASE": "http://127.0.0.1:11434"})

    def test_external_agent_uses_sandbox_and_remaining_deadline(self):
        budget = mock.Mock()
        budget.remaining.return_value = 0.2
        with mock.patch("shutil.which", return_value="/fixture/agent"), mock.patch.object(tools, "sandbox_run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as execute:
            self.assertEqual(runner._run_cli("agent", ["agent"], "workspace", budget), "ok")
        self.assertEqual(execute.call_args.kwargs["timeout"], 0.2)
        self.assertTrue(execute.call_args.kwargs["allow_ollama"])


if __name__ == "__main__":
    unittest.main()
