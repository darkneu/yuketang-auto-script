"""AI mode routing and API regressions; no account or live network required."""

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import openai
from openai import OpenAI

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx

from yuketang.ai import AIError, CodexClient, DeepSeekClient, TYPE_MULTI_CHOICE, create_ai_client
from yuketang.cli import cmd_ai_check, cmd_all, cmd_discussion, cmd_homework
from yuketang.config import Config
from yuketang.course import Course


class AIModeTests(unittest.TestCase):
    def test_config_defaults_to_deepseek_and_normalizes_explicit_mode(self):
        for setting, expected in (({}, "deepseek"), ({"AI_MODE": " CoDeX "}, "codex")):
            with self.subTest(setting=setting), patch.dict(os.environ, setting, clear=True):
                with patch("yuketang.config.load_dotenv"):
                    self.assertEqual(Config.load().ai_mode, expected)

    def test_load_preserves_both_backend_settings_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "AI_MODE=deepseek\nDEEPSEEK_API_KEY=synthetic-key\n"
                "DEEPSEEK_BASE_URL=https://example.invalid/v1\nDEEPSEEK_MODEL=custom-api-model\n"
                "DEEPSEEK_TEMPERATURE=0.2\nDEEPSEEK_TIMEOUT=45\n"
                "CODEX_CLI=custom-codex\nCODEX_MODEL=custom-codex-model\nCODEX_TIMEOUT=90\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AI_MODE": "codex"}, clear=True):
                config = Config.load(str(env_file))
        self.assertEqual(config.ai_mode, "codex")
        self.assertEqual(config.deepseek_api_key, "synthetic-key")
        self.assertEqual(config.deepseek_base_url, "https://example.invalid/v1")
        self.assertEqual(config.deepseek_model, "custom-api-model")
        self.assertEqual(config.deepseek_temperature, 0.2)
        self.assertEqual(config.deepseek_timeout, 45)
        self.assertEqual(config.codex_cli, "custom-codex")
        self.assertEqual(config.codex_model, "custom-codex-model")
        self.assertEqual(config.codex_timeout, 90)
        self.assertNotIn("synthetic-key", repr(config))

    def test_each_mode_requires_only_its_own_configuration(self):
        with patch("yuketang.config.shutil.which", return_value=None):
            config = Config(ai_mode="deepseek", deepseek_api_key="synthetic-key")
            self.assertEqual(config.ai_problems(), [])
            self.assertIsInstance(create_ai_client(config), DeepSeekClient)
        with patch("yuketang.config.shutil.which", return_value="codex.exe"):
            with patch.dict(sys.modules, {"openai": None}):
                config = Config(ai_mode="codex")
                self.assertEqual(config.ai_problems(), [])
                self.assertIsInstance(create_ai_client(config), CodexClient)
        self.assertIn("DEEPSEEK_API_KEY", " ".join(Config(ai_mode="deepseek").ai_problems()))

    def test_unknown_mode_is_rejected_without_selecting_a_backend(self):
        config = Config(ai_mode="typo")
        self.assertIn("AI_MODE", " ".join(config.ai_problems()))
        with self.assertRaisesRegex(AIError, "AI_MODE"):
            create_ai_client(config)

    def test_ai_check_uses_selected_backend_without_login_or_fallback(self):
        for mode, selected, other in (
            ("deepseek", DeepSeekClient, CodexClient),
            ("codex", CodexClient, DeepSeekClient),
        ):
            with self.subTest(mode=mode), patch.object(selected, "chat", return_value="2") as chat:
                with patch.object(other, "chat") as unused, patch("yuketang.cli._ensure_login") as login:
                    self.assertEqual(cmd_ai_check(Config(ai_mode=mode), argparse.Namespace()), 0)
                    chat.assert_called_once()
                    unused.assert_not_called()
                    login.assert_not_called()
        with patch.object(DeepSeekClient, "chat", side_effect=AIError("offline failure")):
            with patch.object(CodexClient, "chat") as unused:
                self.assertEqual(cmd_ai_check(Config(ai_mode="deepseek"), argparse.Namespace()), 1)
                unused.assert_not_called()

    def test_homework_discussion_and_all_receive_the_selected_client(self):
        course = Course("offline course", 1, "test", 1, 1)
        args = argparse.Namespace(course=None, exam=False, speed=None, interval=None)
        for mode, expected in (("deepseek", DeepSeekClient), ("codex", CodexClient)):
            for command, worker_name in (
                (cmd_homework, "HomeworkWorker"),
                (cmd_discussion, "DiscussionWorker"),
                (cmd_all, "HomeworkWorker"),
            ):
                config = Config(ai_mode=mode, deepseek_api_key="synthetic-key", auto_homework=True)
                with self.subTest(mode=mode, command=command.__name__):
                    with patch("yuketang.cli._ensure_login", return_value=MagicMock()) as login:
                        with patch("yuketang.cli._select_courses", return_value=[course]):
                            with patch("yuketang.cli.HomeworkWorker") as homework:
                                with patch("yuketang.cli.DiscussionWorker") as discussion:
                                    with patch("yuketang.cli.VideoWatcher") as video:
                                        with patch("yuketang.cli.RichtextWorker"):
                                            with patch("yuketang.config.shutil.which", return_value="codex.exe"):
                                                video.return_value.watch_course.return_value = {"failed": 0}
                                                self.assertEqual(command(config, args), 0)
                    worker = homework if worker_name == "HomeworkWorker" else discussion
                    self.assertIsInstance(worker.call_args.args[2], expected)
                    login.assert_called_once()


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(ai_mode="deepseek", deepseek_api_key="synthetic-key")
        self.ai = DeepSeekClient(self.config)

    def transport(self, handler):
        sdk = OpenAI(
            api_key="synthetic-key", base_url="https://example.invalid/v1",
            max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(sdk.close)
        self.ai._client = sdk

    @staticmethod
    def response(content):
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": content, "reasoning_content": "must not be used as an answer",
            }}],
        })

    def test_sdk_is_lazy_and_uses_original_api_settings_with_bounded_timeout(self):
        self.config.deepseek_timeout = 45
        with patch("openai.OpenAI") as sdk:
            self.assertIsNone(self.ai._client)
            self.ai._get_client()
            self.ai._get_client()
        sdk.assert_called_once_with(
            api_key="synthetic-key", base_url="https://api.deepseek.com", timeout=45, max_retries=0,
        )

    def test_missing_key_or_sdk_is_actionable(self):
        with self.assertRaisesRegex(AIError, "DEEPSEEK_API_KEY"):
            DeepSeekClient(Config(ai_mode="deepseek")).chat("", "", max_retries=1)
        with patch.dict(sys.modules, {"openai": None}):
            with self.assertRaisesRegex(AIError, "requirements.txt"):
                self.ai.chat("", "", max_retries=1)

    def test_real_sdk_serializes_unicode_and_shared_choice_cache(self):
        sent = []

        def handler(request):
            sent.append(request)
            return self.response("<think>忽略</think>简要分析\n答案：A, C")

        self.transport(handler)
        for _ in range(2):
            self.assertEqual(self.ai.answer(TYPE_MULTI_CHOICE, "中文题干", ["A. 一", "C. 三"]), "A, C")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].url.path, "/v1/chat/completions")
        self.assertEqual(sent[0].headers["authorization"], "Bearer synthetic-key")
        payload = json.loads(sent[0].content)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertIn("中文题干", payload["messages"][1]["content"])
        self.assertEqual(payload["temperature"], 0.1)
        self.assertFalse(payload["stream"])

    def test_fill_judgement_short_and_discussion_use_shared_parsers_and_original_temperatures(self):
        responses = iter(['{"1": "甲", "2": "乙"}', "答案：错误", "简答正文", "讨论正文"])
        temperatures = []

        def handler(request):
            temperatures.append(json.loads(request.content)["temperature"])
            return self.response(next(responses))

        self.transport(handler)
        self.assertEqual(self.ai.answer_fill_blank("题目", 2), {"1": "甲", "2": "乙"})
        self.assertEqual(self.ai.answer_judgement("题目"), "false")
        self.assertEqual(self.ai.answer_short("题目"), "简答正文")
        self.assertEqual(self.ai.answer_discussion("话题"), "讨论正文")
        self.assertEqual(temperatures, [0.1, 0.1, 0.7, 0.8])

    def test_direct_chat_uses_configured_temperature_and_keeps_custom_model(self):
        self.config.deepseek_temperature = 0.25
        self.config.deepseek_model = "custom-model"
        payloads = []
        self.transport(lambda request: (payloads.append(json.loads(request.content)), self.response("正文"))[1])
        self.assertEqual(self.ai.chat("要求", "题目"), "正文")
        self.assertEqual(payloads[0]["temperature"], 0.25)
        self.assertEqual(payloads[0]["model"], "custom-model")

    def test_legacy_reasoner_omits_temperature(self):
        self.config.deepseek_model = "deepseek-reasoner"
        payloads = []
        self.transport(lambda request: (payloads.append(json.loads(request.content)), self.response("正文"))[1])
        self.ai.chat("", "")
        self.assertNotIn("temperature", payloads[0])

    def test_empty_final_content_is_an_error_even_if_reasoning_exists(self):
        self.transport(lambda request: self.response(""))
        with self.assertRaisesRegex(AIError, "未返回答案"):
            self.ai.chat("", "", max_retries=1)

    def test_rate_limit_retries_once_without_sdk_retry_multiplication(self):
        responses = iter([httpx.Response(429, json={"error": {"message": "limit"}}), self.response("2")])
        requests = []

        def handler(request):
            requests.append(request)
            return next(responses)

        self.transport(handler)
        with patch("yuketang.ai.time.sleep") as sleep:
            self.assertEqual(self.ai.chat("", "", max_retries=2), "2")
        self.assertEqual(len(requests), 2)
        sleep.assert_called_once()

    def test_auth_failure_does_not_retry_or_expose_response_body(self):
        self.transport(lambda request: httpx.Response(401, json={"error": {"message": "synthetic-key"}}))
        with patch("yuketang.ai.time.sleep") as sleep:
            with self.assertRaisesRegex(AIError, "HTTP 401") as error:
                self.ai.chat("", "")
        self.assertNotIn("synthetic-key", str(error.exception))
        sleep.assert_not_called()

    def test_ctrl_c_propagates_without_retry(self):
        self.ai._client = MagicMock()
        create = self.ai._client.chat.completions.create
        create.side_effect = KeyboardInterrupt
        with patch("yuketang.ai.time.sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.ai.chat("", "")
        create.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
