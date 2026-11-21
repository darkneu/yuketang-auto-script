"""Saved login, school setup routing, and local AI reply logs; no live calls."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from yuketang.ai import CodexClient, DeepSeekClient
from yuketang.ai_log import AIReplyLog
from yuketang.config import Config
from yuketang.course import Course
from yuketang.course import Leaf
from yuketang.desktop import MainWindow, SettingsDialog, configure_app
from yuketang.desktop_backend import DesktopService
from yuketang.desktop_runtime import _worker
from yuketang.login import LoginError
from yuketang.homework import HomeworkWorker


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def settle(self, predicate=lambda: True):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                break
            time.sleep(0.005)

    def test_saved_login_starts_once_without_button_and_loads_courses(self):
        service = DesktopService(Config(session_id="offline", university_id="123"))
        def login():
            service.session = Mock()
            service.emit("courses", [Course("offline course", 1, "", 1, 1)])
        with patch.object(service, "login", side_effect=login) as restored, \
             patch("requests.sessions.Session.request") as network:
            window = MainWindow(service=service)
            self.addCleanup(window.close)
            window.show()
            self.settle(lambda: window.course_list.count() == 1 and not service.busy)
            self.assertEqual(window.pages.currentIndex(), 1)
            self.assertEqual(window.course_list.count(), 1)
            restored.assert_called_once_with()
            network.assert_not_called()
            self.assertFalse(hasattr(window, "reuse_btn"))

    def test_missing_school_routes_primary_button_to_connection_tab_without_login(self):
        service = DesktopService(Config(session_id="offline"))
        seen = []
        def visit(dialog):
            seen.append(dialog.tabs.currentIndex())
            return QDialog.Rejected
        with patch.object(service, "login") as login, patch.object(SettingsDialog, "exec", visit):
            window = MainWindow(service=service)
            self.addCleanup(window.close)
            window.show()
            self.settle()
            self.assertEqual(window.login_btn.text(), "配置学校ID")
            QTest.mouseClick(window.login_btn, Qt.LeftButton)
            self.assertEqual(seen, [2])
            login.assert_not_called()

    def test_saving_school_updates_primary_button(self):
        service = DesktopService(Config())
        window = MainWindow(service=service)
        self.addCleanup(window.close)
        def save(dialog):
            dialog.result_config = Config(university_id="123")
            return QDialog.Accepted
        with patch.object(SettingsDialog, "exec", save), patch.object(service, "login") as login:
            window.login_action()
        self.assertEqual(window.login_btn.text(), "微信扫码登录")
        self.assertEqual(service.config.university_id, "123")
        login.assert_not_called()

    def test_failed_restore_does_not_loop_and_scan_button_becomes_available(self):
        service = DesktopService(Config(session_id="offline", university_id="123"))
        with patch.object(service, "login", side_effect=LoginError("离线登录失败")) as login:
            window = MainWindow(service=service)
            self.addCleanup(window.close)
            window.show()
            self.settle(lambda: login.called and not service.busy)
            self.settle()
        login.assert_called_once_with()
        self.assertTrue(window.login_btn.isEnabled())
        self.assertIn("离线登录失败", window.status.text())

    def test_closed_window_cannot_start_queued_automatic_login(self):
        service = DesktopService(Config(session_id="offline", university_id="123"))
        with patch.object(service, "login") as login:
            window = MainWindow(service=service)
            window.close()
            self.settle()
        login.assert_not_called()

    def test_task_count_has_no_extra_status_caption(self):
        window = MainWindow(demo=True)
        self.addCleanup(window.close)
        self.assertEqual(window.summary.text(), "7 项任务")


class ReplyLogTests(unittest.TestCase):
    def test_both_backends_log_raw_reply_once_before_cleaning_without_credentials(self):
        for mode in ("deepseek", "codex"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "logs" / "ai-replies.jsonl"
                log = AIReplyLog(path)
                config = Config(ai_mode=mode, deepseek_api_key="private-api-key", session_id="private-cookie")
                raw = "<think>附带文本</think>简要分析\n答案：A"
                client = DeepSeekClient(config) if mode == "deepseek" else CodexClient(config)
                client.reply_observer = log.record
                try:
                    if mode == "deepseek":
                        client._client = Mock()
                        client._client.chat.completions.create.return_value = Mock(choices=[Mock(message=Mock(content=raw))])
                        self.assertEqual(client.answer_choice("题干", ["A. 选项"]), "A")
                        client._client.chat.completions.create.assert_called_once()
                    else:
                        def respond(args, **kwargs):
                            Path(args[args.index("--output-last-message") + 1]).write_text(raw, encoding="utf-8")
                            return subprocess.CompletedProcess(args, 0, "", "private-stderr")
                        with patch.object(client, "_command", return_value=["offline.exe"]), \
                             patch.object(client, "_run_codex", side_effect=respond) as run:
                            self.assertEqual(client.answer_choice("题干", ["A. 选项"]), "A")
                        run.assert_called_once()
                finally:
                    log.close()
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 1)
                record = json.loads(lines[0])
                self.assertEqual(record["reply"], raw)
                self.assertEqual(record["backend"], mode)
                self.assertIn("题干", record["question"])
                self.assertTrue(record["time"])
                self.assertNotIn("private", lines[0])
                self.assertFalse(log.failed)

    def test_logs_append_and_no_file_is_created_without_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ai-replies.jsonl"
            empty = AIReplyLog(path)
            empty.close()
            self.assertFalse(path.exists())
            for value in ("first", "second"):
                log = AIReplyLog(path)
                log.record({"reply": value})
                log.close()
            self.assertEqual([json.loads(line)["reply"] for line in path.read_text().splitlines()], ["first", "second"])

    def test_slow_file_io_does_not_block_reply_or_task_thread(self):
        entered, release = threading.Event(), threading.Event()
        log = AIReplyLog(Path("unused-offline-log"))
        def open_slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            return Mock()
        with patch.object(Path, "open", side_effect=open_slow), patch.object(Path, "mkdir"):
            try:
                log.record({"reply": "first"})
                self.assertTrue(entered.wait(1))
                start = time.monotonic()
                for i in range(100):
                    log.record({"reply": str(i)})
                self.assertLess(time.monotonic() - start, 0.5)
            finally:
                release.set()
                log.close()

    def test_unwritable_log_does_not_repeat_ai_request_or_change_answer(self):
        client = DeepSeekClient(Config(deepseek_api_key="offline"))
        client._client = Mock()
        client._client.chat.completions.create.return_value = Mock(choices=[Mock(message=Mock(content="答案：A"))])
        log = AIReplyLog(Path("unused-offline-log"))
        client.reply_observer = log.record
        with patch.object(Path, "mkdir", side_effect=PermissionError):
            try:
                self.assertEqual(client.answer_choice("题干", ["A. 选项"]), "A")
            finally:
                log.close()
        self.assertTrue(log.failed)
        client._client.chat.completions.create.assert_called_once()

    def test_isolated_connection_check_wires_reply_log_and_flushes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(deepseek_api_key="offline")
            sender = Mock()
            def reply(*args, **kwargs):
                client.reply_observer({"reply": "2"})
                return "2"
            client = Mock()
            client.chat.side_effect = reply
            with patch("yuketang.config.PROJECT_ROOT", Path(directory)), \
                 patch("yuketang.ai.create_ai_client", return_value=client), \
                 patch("yuketang.desktop_runtime.os.setsid", create=True):
                _worker("ai_check", config, sender, threading.Event(), {})
            path = Path(directory) / "logs" / "ai-replies.jsonl"
            self.assertEqual(json.loads(path.read_text())["reply"], "2")
            sender.send.assert_called_once_with(("ai_check", (True, "连接正常")))

    def test_isolated_homework_logs_reply_from_existing_ai_call(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(auto_homework=True, auto_video=False, auto_richtext=False,
                            deepseek_api_key="private-key")
            client = DeepSeekClient(config)
            client._client = Mock()
            client._client.chat.completions.create.return_value = Mock(choices=[Mock(message=Mock(content="作业回复原文"))])
            cancel = threading.Event()
            data = {"problems": [{"problem_id": 1, "content": {"Type": "ShortAnswer", "Body": "<p>题目正文</p>"}}]}
            with patch("yuketang.config.PROJECT_ROOT", Path(directory)), \
                 patch("yuketang.desktop_backend.create_ai_client", return_value=client), \
                 patch.object(HomeworkWorker, "get_problems", return_value=data), \
                 patch.object(HomeworkWorker, "_submit", return_value=True) as submit, \
                 patch.object(cancel, "wait", return_value=False), \
                 patch("yuketang.desktop_runtime.os.setsid", create=True):
                _worker("course", config, Mock(), cancel, {
                    "session_data": {"domain": "example.invalid"},
                    "course": Course("offline", 1, "", 1, 1), "leaves": [Leaf(1, "homework", 6)]})
            record = json.loads((Path(directory) / "logs" / "ai-replies.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["reply"], "作业回复原文")
            self.assertEqual(record["question"], "题目正文")
            client._client.chat.completions.create.assert_called_once()
            submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
