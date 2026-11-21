"""Codex process boundary tests; no account or network required."""

import os
import _thread
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from yuketang.ai import AIError, CodexClient, TYPE_MULTI_CHOICE
from yuketang.config import Config


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.client = CodexClient(Config(codex_model="test-model", codex_timeout=12))
        self.command = patch.object(self.client, "_command", return_value=["codex.exe"])
        self.command.start()
        self.addCleanup(self.command.stop)

    def response(self, text):
        def run(args, **kwargs):
            output = Path(args[args.index("--output-last-message") + 1])
            output.write_text(text, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="progress, not the answer", stderr="")
        return run

    def test_unicode_prompt_uses_stdin_and_final_file(self):
        question = '中文题目 & $(echo example) "测试"'
        with patch.object(self.client, "_run_codex", side_effect=self.response("答案：A")) as run:
            self.assertEqual(self.client.chat("要求", question), "答案：A")
        args, kwargs = run.call_args
        self.assertNotIn(question, args[0])
        self.assertIn(question, kwargs["input"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["timeout"], 12)
        self.assertIn("test-model", args[0])
        self.assertEqual(args[0][args[0].index("--sandbox") + 1], "read-only")
        self.assertNotEqual(Path(kwargs["cwd"]), self.client.config.project_root)
        self.assertFalse(Path(kwargs["cwd"]).exists())

    def test_blank_model_inherits_cli_default(self):
        self.client.config.codex_model = ""
        with patch.object(self.client, "_run_codex", side_effect=self.response("2")) as run:
            self.client.chat("", "1+1")
        self.assertNotIn("--model", run.call_args.args[0])

    def test_empty_response_is_error_instead_of_guessed_answer(self):
        with patch.object(self.client, "_run_codex", side_effect=self.response("<think>分析</think>")):
            with self.assertRaisesRegex(AIError, "未返回答案"):
                self.client.chat("", "", max_retries=1)

    def test_nonzero_exit_reports_login_guidance(self):
        result = subprocess.CompletedProcess([], 1, "", "not logged in")
        with patch.object(self.client, "_run_codex", return_value=result):
            with self.assertRaisesRegex(AIError, "codex login"):
                self.client.chat("", "", max_retries=1)

    def test_timeout_is_reported(self):
        with patch.object(self.client, "_run_codex", side_effect=subprocess.TimeoutExpired("codex", 12)):
            with self.assertRaisesRegex(AIError, "CODEX_TIMEOUT"):
                self.client.chat("", "", max_retries=1)

    def test_choice_parsing_and_cache_survive_backend_change(self):
        with patch.object(self.client, "_run_codex", side_effect=self.response("简要分析\n答案：A, C")) as run:
            for _ in range(2):
                self.assertEqual(self.client.answer(TYPE_MULTI_CHOICE, "题干", ["A. 一", "C. 三"]), "A, C")
        run.assert_called_once()

    def test_config_loads_codex_settings_without_api_key(self):
        with patch.dict(os.environ, {"CODEX_CLI": "custom-codex", "CODEX_MODEL": "custom-model", "CODEX_TIMEOUT": "90"}, clear=True):
            with patch("yuketang.config.load_dotenv"):
                config = Config.load()
        self.assertEqual(config.codex_cli, "custom-codex")
        self.assertEqual(config.codex_model, "custom-model")
        self.assertEqual(config.codex_timeout, 90)

    def test_real_child_process_preserves_unicode_stdin_and_stdout(self):
        child = "import sys; print(sys.stdin.buffer.read().decode('utf-8'))"
        result = CodexClient._run_codex(
            [sys.executable, "-X", "utf8", "-c", child],
            input="中文题目", timeout=5, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "中文题目")

    def test_ctrl_c_stops_waiting_child_without_retry(self):
        timer = threading.Timer(0.5, _thread.interrupt_main)
        started = time.monotonic()
        with patch.object(CodexClient, "_stop_process", wraps=CodexClient._stop_process) as stop:
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    CodexClient._run_codex(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        input="", timeout=30, text=True,
                    )
            finally:
                timer.cancel()
                timer.join()
        stop.assert_called_once()
        self.assertIsNotNone(stop.call_args.args[0].poll())
        self.assertLess(time.monotonic() - started, 8)

    def test_timeout_terminates_real_child(self):
        with patch.object(CodexClient, "_stop_process", wraps=CodexClient._stop_process) as stop:
            with self.assertRaises(subprocess.TimeoutExpired):
                CodexClient._run_codex(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    input="", timeout=0.3, text=True,
                )
        stop.assert_called_once()
        self.assertIsNotNone(stop.call_args.args[0].poll())


if __name__ == "__main__":
    unittest.main()
