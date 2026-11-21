"""Regression checks for native layout, live history and bounded task cancellation."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import ctypes
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from dotenv import dotenv_values
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel
from yuketang.config import Config
from yuketang.course import Course, Leaf
from yuketang.desktop import MainWindow, SettingsDialog, configure_app
from yuketang.desktop_backend import DesktopService
from yuketang.desktop_runtime import run_isolated, _worker
from yuketang.desktop_widgets import TaskSwitch
from yuketang.homework import HomeworkWorker
from yuketang.session import YuketangSession


def stubborn_worker(kind, config, sender, cancel, payload):
    if os.name != "nt":
        os.setsid()
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
    sender.send(("ready", child.pid))
    time.sleep(60)  # Deliberately ignore cancel, like a stuck HTTP SDK call.


def pid_alive(pid):
    if os.name == "nt":
        api = ctypes.windll.kernel32
        api.OpenProcess.restype = ctypes.c_void_p
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 258
        finally:
            api.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class IsolationTests(unittest.TestCase):
    def test_stubborn_job_and_descendant_are_stopped_without_touching_other_jobs(self):
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **flags)
        stop, events = threading.Event(), []
        def emit(kind, value):
            events.append((kind, value))
            if kind == "ready":
                stop.set()
        started = time.monotonic()
        try:
            run_isolated("offline", Config(), stop, emit, worker=stubborn_worker, grace=0.2)
            self.assertLess(time.monotonic() - started, 6)
            self.assertIn(("cancelled", None), events)
            child_pid = next(value for kind, value in events if kind == "ready")
            self.assertFalse(pid_alive(child_pid))
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_pre_cancelled_job_does_not_launch(self):
        stop = threading.Event()
        stop.set()
        with patch("yuketang.desktop_runtime._bootstrap") as bootstrap:
            run_isolated("offline_probe", Config(), stop, Mock())
        bootstrap.assert_not_called()

    def test_ai_check_uses_unsaved_selected_config_and_only_one_request(self):
        sender, client = Mock(), Mock()
        cfg = Config(ai_mode="codex", codex_model="draft-model")
        cancel = threading.Event()
        with patch("yuketang.ai.create_ai_client", return_value=client) as factory:
            with patch("yuketang.desktop_runtime.os.setsid", create=True):
                _worker("ai_check", cfg, sender, cancel, {})
        factory.assert_called_once_with(cfg)
        client.chat.assert_called_once()
        self.assertEqual(client.chat.call_args.kwargs["max_retries"], 1)
        self.assertIs(client.stop_event, cancel)
        sender.send.assert_called_once_with(("ai_check", (True, "连接正常")))

    def test_logout_clears_auth_but_preserves_school_ai_and_other_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("SESSION_ID=old\nCSRF_TOKEN=old\nUNIVERSITY_ID=school\nDEEPSEEK_API_KEY=synthetic\nCUSTOM=yes\n", encoding="utf-8")
            service = DesktopService(Config(session_id="old", csrf_token="old", user_id="42"))
            service.session = YuketangSession("example.invalid", cookies={"sessionid": "old"})
            service.courses = [Course("offline", 1, "", 1, 1)]
            with patch("yuketang.config.PROJECT_ROOT", root):
                service.logout()
            values = dotenv_values(root / ".env")
            self.assertEqual(values["SESSION_ID"], "")
            self.assertEqual(values["CSRF_TOKEN"], "")
            self.assertEqual(values["UNIVERSITY_ID"], "school")
            self.assertEqual(values["DEEPSEEK_API_KEY"], "synthetic")
            self.assertEqual(values["CUSTOM"], "yes")
            self.assertIsNone(service.session)
            self.assertFalse(service.config.has_credentials)
            self.assertFalse(service.courses)

    def test_each_task_type_is_independent_including_video(self):
        cfg = Config(auto_video=False, auto_richtext=False, auto_homework=True)
        service = DesktopService(cfg)
        service.leaves = [Leaf(kind, "offline", kind) for kind in (0, 3, 4, 5, 6)]
        self.assertEqual([leaf.leaf_type for leaf in service.selected_leaves()], [6])
        cfg.auto_homework = False
        self.assertEqual(service.selected_leaves(), [])
        cfg.auto_exam = True
        self.assertEqual([leaf.leaf_type for leaf in service.selected_leaves()], [5])

    def test_answer_events_distinguish_generated_submitted_and_prior_unknown_answers(self):
        session = YuketangSession("example.invalid")
        self.addCleanup(session.session.close)
        events = []
        session.observer = lambda leaf_id, values: events.append(values["problem"])
        ai = Mock()
        ai.answer.return_value = "A"
        worker = HomeworkWorker(session, Config(), ai)
        data = {"problems": [
            {"problem_id": 1, "content": {"Type": "SingleChoice", "Body": "new"}},
            {"problem_id": 2, "content": {"Type": "SingleChoice", "Body": "old"}, "user": {"my_count": 1, "count": 1}}]}
        with patch.object(worker, "get_problems", return_value=data), patch.object(worker, "_submit", return_value=True), patch.object(session.stop_event, "wait", return_value=False):
            worker.solve_leaf(Course("offline", 1, "", 1, 1), Leaf(6, "offline", 6))
        self.assertTrue(any(p["state"] == "提交中" and p["answer"] == "A" for p in events))
        self.assertTrue(any(p["state"] == "已提交" and p["answer"] == "A" for p in events))
        old = next(p for p in events if p["id"] == 2)
        self.assertNotIn("answer", old)
        ai.answer.assert_called_once()


class LiveUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def setUp(self):
        self.window = MainWindow(demo=True)
        self.window.show()
        self.settle()
        self.addCleanup(self.window.close)

    def settle(self):
        for _ in range(12):
            self.app.processEvents()

    def test_long_course_titles_keep_uniform_spacing_and_no_horizontal_scroll(self):
        courses = [Course("非常长的课程名称" * (index + 1), index, "", index, index) for index in range(4)]
        self.window.on_event("courses", courses)
        self.settle()
        view = self.window.course_list
        rectangles = [view.visualItemRect(view.item(index)) for index in range(4)]
        self.assertEqual(len({rect.height() for rect in rectangles}), 1)
        self.assertEqual(len({rectangles[i+1].top() - rectangles[i].bottom() for i in range(3)}), 1)
        self.assertFalse(view.horizontalScrollBar().isVisible())
        self.assertEqual(view.item(0).toolTip(), courses[0].course_name)
        texts = [widget.text() for widget in self.window.findChildren(QLabel)]
        self.assertNotIn("已登录", texts)
        self.assertNotIn("桌面助手 / 本地运行", texts)
        self.assertFalse(hasattr(self.window, "detail"))

    def test_five_switches_gear_and_no_selection_disable_run(self):
        dialog = SettingsDialog(Config(), self.window, demo=True)
        self.assertEqual(len(dialog.findChildren(TaskSwitch)), 5)
        for key in ("auto_video", "auto_richtext", "auto_discussion", "auto_homework", "auto_exam"):
            dialog.fields[key].setChecked(False)
        self.assertFalse(any(dialog.values()[key] for key in ("auto_video", "auto_richtext", "auto_discussion", "auto_homework", "auto_exam")))
        dialog.close()
        seen = []
        def visit(dialog):
            seen.append(dialog.tabs.currentIndex())
            return QDialog.Rejected
        with patch.object(SettingsDialog, "exec", visit):
            QTest.mouseClick(self.window.task_settings_btn, Qt.LeftButton)
        self.assertEqual(seen, [1])
        self.assertNotIn("已启用", self.window.enabled.text())
        cfg = self.window.service.config
        cfg.auto_video = cfg.auto_richtext = False
        self.window.start()
        self.assertFalse(self.window.service.busy)
        self.assertIn("选择", self.window.status.text())

    def test_logout_confirmation_can_cancel_then_clear_all_account_views(self):
        with patch("yuketang.desktop.LogoutDialog.exec", return_value=QDialog.Rejected):
            self.window.logout()
        self.assertTrue(self.window.service.courses)
        with patch("yuketang.desktop.LogoutDialog.exec", return_value=QDialog.Accepted):
            self.window.logout()
        self.assertEqual(self.window.course_list.count(), 0)
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.assertFalse(self.window.states)
        self.assertFalse(self.window.run_panel.cards)
        self.assertFalse(self.window.start_btn.isEnabled())

    def test_runtime_history_stats_answers_and_follow_tail(self):
        cfg = self.window.service.config
        cfg.auto_video = cfg.auto_richtext = False
        cfg.auto_homework = True
        self.window.start()
        self.window.demo_timer.stop()
        self.assertEqual(self.window.task_views.currentIndex(), 1)
        self.assertEqual(self.window.active_ids, {6})
        panel = self.window.run_panel
        for index in range(16):
            self.window.on_event("task", (6, {"state": "处理中", "problem": {
                "id": index, "type": "ShortAnswer", "body": "离线题目\n" * 5,
                "state": "已提交", "answer": f"离线答案 {index}"}}))
            self.settle()
        bar = panel.scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertEqual(len(panel.cards[6].problems), 16)
        bar.setValue(bar.maximum() // 3)
        saved_position = bar.value()
        self.window.on_event("task", (6, {"state": "处理中", "problem": {
            "id": 16, "type": "ShortAnswer", "body": "new\n" * 10, "state": "生成答案中"}}))
        self.settle()
        self.assertFalse(panel.follow_tail)
        self.assertEqual(bar.value(), saved_position)
        bar.setValue(bar.maximum())
        self.window.on_event("task", (6, {"state": "已处理", "outcome": "success", "problem": {
            "id": 16, "type": "ShortAnswer", "body": "new\n" * 10, "state": "已提交", "answer": "final\n" * 20}}))
        self.settle()
        self.assertEqual(bar.value(), bar.maximum())
        self.assertIn("已完成 1", panel.stats.text())
        self.assertIn("final", panel.cards[6].problems[16][2].text())
        self.window.stop()
        self.assertEqual(self.window.states[6]["state"], "已处理")

    def test_progress_does_not_overlay_placeholder_and_row_fits_label(self):
        self.window.on_event("task", (1, {"state": "处理中", "position": 798, "duration": 1168}))
        self.settle()
        item = self.window.items[1]
        widget = self.window.tree.itemWidget(item, 2)
        text, progress = self.window.progress_widgets[1]
        self.assertEqual(item.text(2), "")
        self.assertGreaterEqual(widget.height(), text.height() + progress.height() + 4)
        self.assertEqual(text.text(), "13:18 / 19:28")

    def test_ai_check_requires_click_and_browse_changes_only_draft(self):
        with patch("yuketang.desktop_runtime.run_isolated") as run:
            dialog = SettingsDialog(Config(), self.window, demo=True)
            run.assert_not_called()
            dialog.check_connection()
            run.assert_not_called()
            self.assertIn("演示模式", dialog.check_status.text())
            with patch("yuketang.desktop.QFileDialog.getOpenFileName", return_value=("C:/Tools/codex.exe", "")):
                dialog.browse_codex()
            self.assertEqual(dialog.fields["codex_cli"].text(), "C:/Tools/codex.exe")
            self.assertEqual(dialog.config.codex_cli, "codex")
            dialog.close()

    def test_explicit_ai_check_uses_current_unsaved_fields(self):
        with patch("yuketang.desktop_runtime.run_isolated") as run:
            dialog = SettingsDialog(Config(deepseek_api_key="saved-key"), self.window)
            run.assert_not_called()
            dialog.fields["deepseek_model"].setText("draft-model")
            dialog.key.setText("draft-key")
            run.side_effect = lambda kind, cfg, stop, emit: emit("ai_check", (True, "连接正常"))
            dialog.check_connection()
            dialog.check_job.join(1)
            self.settle()
            self.assertEqual(run.call_args.args[0], "ai_check")
            candidate = run.call_args.args[1]
            self.assertEqual(candidate.deepseek_model, "draft-model")
            self.assertEqual(candidate.deepseek_api_key, "draft-key")
            self.assertEqual(dialog.config.deepseek_api_key, "saved-key")
            self.assertEqual(dialog.check_status.text(), "连接正常")
            dialog.close()

    def test_close_during_stubborn_isolated_job_remains_responsive_and_finishes(self):
        self.window.close()
        service = DesktopService(Config())
        window = MainWindow(service=service)
        window.show()
        ready = threading.Event()
        def operation():
            def emit(kind, value):
                if kind == "ready":
                    ready.set()
            run_isolated("offline", Config(), service.cancel, emit, worker=stubborn_worker, grace=0.2)
        window.launch("离线卡住请求", operation)
        deadline = time.monotonic() + 8
        while not ready.is_set() and time.monotonic() < deadline:
            self.settle()
            time.sleep(0.01)
        self.assertTrue(ready.is_set())
        started = time.monotonic()
        window.close()
        heartbeats = 0
        while window.isVisible() and time.monotonic() - started < 6:
            self.settle()
            heartbeats += 1
            time.sleep(0.01)
        self.assertFalse(window.isVisible())
        self.assertFalse(service.busy)
        self.assertFalse(window.job.is_alive())
        self.assertGreater(heartbeats, 2)
        self.assertLess(time.monotonic() - started, 6)
        window.close()


if __name__ == "__main__":
    unittest.main()
