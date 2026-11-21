"""Durable, account-isolated display history without extra requests or task skipping."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QDialog

from yuketang.config import Config
from yuketang.course import Course, Leaf
from yuketang.desktop import MainWindow, configure_app
from yuketang.desktop_backend import DesktopService
from yuketang.desktop_state import TaskStateStore, progress_caption
from yuketang.session import YuketangSession
from yuketang.video import VideoWatcher


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.config = SimpleNamespace(project_root=Path(self.temp))
        self.session = SimpleNamespace(domain="example.invalid", university_id="school-a", user_id="account-a")
        self.course = Course("offline", 1, "", 2, 3)
        self.leaf = Leaf(1, "offline video", 0, sku_id=2, leafinfo_id=3)
        self.store = TaskStateStore()
        self.store.open_course(self.config, self.session, self.course, [self.leaf])

    def save_complete(self):
        self.store.remember(1, {"server_rate": 1.0, "server_completed": True})
        self.store.remember(1, {"state": "服务端已完成", "outcome": "success"})
        self.assertTrue(self.store.flush())

    def reopen(self, **kwargs):
        return TaskStateStore().open_course(kwargs.get("config", self.config), kwargs.get("session", self.session),
                                           kwargs.get("course", self.course), kwargs.get("leaves", [self.leaf]))

    def test_new_instance_restores_dated_completion_without_live_outcome(self):
        self.save_complete()
        restored = self.reopen()[1]
        self.assertEqual(restored["state"], "上次已完成")
        self.assertEqual(restored["cached_server_rate"], 1.0)
        self.assertTrue(restored["cache_checked_at"])
        self.assertNotIn("outcome", restored)
        self.assertNotIn("server_rate", restored)
        self.assertIn("本次尚未核实", progress_caption(restored))

    def test_accounts_domains_schools_courses_and_replaced_leaves_are_isolated(self):
        self.save_complete()
        for field in ("user_id", "domain", "university_id"):
            values = vars(self.session).copy()
            values[field] += "-other"
            with self.subTest(field=field):
                self.assertEqual(self.reopen(session=SimpleNamespace(**values)), {})
        for field in ("classroom_id", "course_id", "sku_id"):
            with self.subTest(field=field):
                self.assertEqual(self.reopen(course=replace(self.course, **{field: 99})), {})
        for field, value in (("leaf_type", 6), ("leafinfo_id", 99), ("sku_id", 99), ("name", "replacement")):
            with self.subTest(field=field):
                self.assertEqual(self.reopen(leaves=[replace(self.leaf, **{field: value})]), {})

    def test_unknown_identity_never_reads_or_writes_another_accounts_history(self):
        self.save_complete()
        for field in ("user_id", "university_id"):
            values = vars(self.session).copy()
            values[field] = ""
            store = TaskStateStore()
            with patch.object(Path, "read_text", side_effect=AssertionError("must not read")):
                self.assertEqual(store.open_course(self.config, SimpleNamespace(**values), self.course, [self.leaf]), {})
            self.assertFalse(store.remember(1, {"state": "已处理", "outcome": "success"}))
            self.assertIsNone(store.path)

    def test_transient_deltas_never_dirty_cache_or_store_questions_credentials(self):
        for index in range(100):
            self.assertFalse(self.store.remember(1, {"state": "处理中", "position": index, "stage": "提交中",
                "problem": {"body": "private question", "answer": "private answer"}, "token": "private credential"}))
        self.assertFalse(self.store.dirty)
        self.assertFalse(self.store.path.exists())
        self.store.remember(1, {"state": "已处理", "outcome": "success", "answer": "private answer",
                                "content": "private question", "cookies": "private credential"})
        self.assertTrue(self.store.flush())
        text = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("private", text)
        self.assertNotIn("account-a", text)
        self.assertNotIn("school-a", text)

    def test_partial_server_snapshot_survives_without_a_final_result(self):
        self.store.remember(1, {"server_rate": 0.32, "server_completed": False})
        self.store.flush()
        restored = self.reopen()[1]
        self.assertEqual(restored["state"], "上次未完成")
        self.assertIn("32.0%", progress_caption(restored))

    def test_new_server_snapshot_supersedes_old_completion(self):
        self.save_complete()
        self.store.remember(1, {"server_rate": 0.25, "server_completed": False})
        self.store.flush()
        self.assertEqual(self.reopen()[1]["state"], "上次未完成")
        self.assertNotIn("result", self.store.records["1"])

    def test_failed_locked_stopped_and_processed_results_keep_distinct_history(self):
        for state, outcome, caption in (("已处理", "success", "上次已处理"), ("已跳过", "skipped", "上次已作答"),
                                        ("考试已锁定", "unprocessed", "上次未完成"), ("部分失败", "failed", "上次未完成"),
                                        ("已停止", "stopped", "上次已停止")):
            with self.subTest(state=state):
                self.store.remember(1, {"state": state, "outcome": outcome})
                self.store.flush()
                restored = self.reopen()[1]
                self.assertEqual(restored["state"], caption)
                self.assertEqual(restored["cached_state"], state)
                self.assertNotIn("outcome", restored)

    def test_corrupt_unknown_version_and_invalid_progress_do_not_break_loading(self):
        self.save_complete()
        for invalid in ("{", "[]", '{"version":99,"tasks":{}}',
                        json.dumps({"version": 1, "tasks": {"1": {"fingerprint": self.store.fingerprints["1"],
                            "progress": {"server_rate": "invalid", "at": "not a date"}}}})):
            with self.subTest(invalid=invalid):
                self.store.path.write_text(invalid, encoding="utf-8")
                self.assertEqual(self.reopen(), {})

    def test_atomic_write_failure_preserves_previous_file_and_can_retry(self):
        self.save_complete()
        before = self.store.path.read_bytes()
        self.store.remember(1, {"state": "处理失败", "outcome": "failed"})
        with patch("yuketang.desktop_state.os.replace", side_effect=PermissionError):
            self.assertFalse(self.store.flush())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(self.store.dirty)
        self.assertFalse(list(self.store.path.parent.glob("*.tmp")))
        self.assertTrue(self.store.flush())
        self.assertEqual(self.reopen()[1]["state"], "上次未完成")

    def test_course_switch_flushes_pending_results(self):
        self.store.remember(1, {"state": "已处理", "outcome": "success"})
        self.store.open_course(self.config, self.session, replace(self.course, classroom_id=99), [self.leaf])
        self.assertEqual(self.reopen()[1]["state"], "上次已处理")


class StateUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("yuketang.config.PROJECT_ROOT", Path(directory)))
        self.network = self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("no network")))
        self.course = Course("offline", 1, "", 1, 1)
        self.leaves = [Leaf(1, "video", 0), Leaf(2, "homework", 6), Leaf(3, "exam", 5)]
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in self.windows:
            window.close()

    def window(self, account="account-a"):
        service = DesktopService(Config(university_id="school-a"))
        service.session = YuketangSession("example.invalid", university_id="school-a", user_id=account)
        window = MainWindow(service=service)
        self.windows.append(window)
        window.on_event("leaves", (self.course, self.leaves))
        return window

    @staticmethod
    def complete(window):
        window.on_event("task", (1, {"server_rate": 1.0, "server_completed": True}))
        window.on_event("task", (1, {"state": "服务端已完成", "outcome": "success"}))
        window.on_event("task", (2, {"state": "已处理", "outcome": "success"}))
        window.on_event("task", (3, {"state": "考试已锁定", "outcome": "unprocessed"}))

    def test_relogin_course_reload_and_app_restart_restore_history(self):
        first = self.window()
        self.complete(first)
        # Reload before the coalescing timer fires; switching must still flush pending data.
        first.on_event("leaves", (self.course, self.leaves))
        self.assertEqual(first.items[1].text(1), "上次已完成")
        self.assertIn("本次尚未核实", first.items[1].toolTip(1))
        self.assertEqual(first.items[1].text(2), "上次完成率 100.0%")
        with patch("yuketang.desktop.LogoutDialog.exec", return_value=QDialog.Accepted):
            first.logout()
        self.assertFalse(first.states)
        second = self.window()
        self.assertEqual(second.states[1]["state"], "上次已完成")
        self.assertEqual(second.states[2]["state"], "上次已处理")
        self.assertEqual(second.states[3]["state"], "上次未完成")
        self.assertFalse(second.run_panel.cards)
        second.close()
        third = self.window()
        self.assertEqual(third.items[1].text(1), "上次已完成")
        self.assertFalse(self.window(account="account-b").states)
        self.network.assert_not_called()

    def test_cached_completion_does_not_skip_worker_or_count_as_this_run(self):
        first = self.window()
        self.complete(first)
        first.close()
        second = self.window()
        # Launch synchronously with controlled business worker; never contact a real account.
        with patch.object(second, "launch", side_effect=lambda title, operation: operation()), \
             patch("yuketang.desktop_runtime.run_isolated", side_effect=lambda *args, **kwargs: second.service.run_course()), \
             patch.object(VideoWatcher, "_watch_one", return_value="failed") as watch:
            second.start()
        self.app.processEvents()  # Video worker signals cross a Qt thread boundary.
        watch.assert_called_once()
        self.assertEqual(second.states[1]["state"], "未确认完成")
        self.assertEqual(second.states[1]["outcome"], "failed")
        self.assertIn("已完成 0", second.run_panel.stats.text())

    def test_save_failure_shows_notice_without_changing_success(self):
        window = self.window()
        self.complete(window)
        with patch("yuketang.desktop_state.os.replace", side_effect=PermissionError):
            window.on_event("run_done", 3)
        self.assertEqual(window.states[1]["outcome"], "success")
        self.assertIn("状态记录保存失败", window.status.text())
        self.assertTrue(window.task_state_store.dirty)
        window.flush_task_states()

    def test_question_events_do_not_schedule_writes_and_final_events_are_coalesced(self):
        window = self.window()
        with patch.object(window.state_save_timer, "start", wraps=window.state_save_timer.start) as start:
            for index in range(100):
                window.on_event("task", (2, {"state": "处理中", "problem": {
                    "id": index, "state": "生成答案中", "type": "ShortAnswer", "body": "offline"}}))
            start.assert_not_called()
            window.on_event("task", (2, {"state": "已处理", "outcome": "success"}))
            window.on_event("task", (3, {"state": "考试已锁定", "outcome": "unprocessed"}))
            start.assert_called_once()

    def test_demo_never_reads_or_writes_real_history(self):
        with patch.object(TaskStateStore, "open_course", side_effect=AssertionError("must not open cache")), \
             patch.object(TaskStateStore, "remember", side_effect=AssertionError("must not save cache")):
            demo = MainWindow(demo=True)
            self.windows.append(demo)
            demo.on_event("task", (1, {"state": "服务端已完成", "outcome": "success"}))
            demo.close()


if __name__ == "__main__":
    unittest.main()
