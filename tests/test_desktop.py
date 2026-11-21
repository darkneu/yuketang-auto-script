"""Offline desktop integration tests. No platform, account or real AI calls."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dotenv import dotenv_values
from PySide6.QtWidgets import QApplication, QLineEdit, QLabel, QStyle, QStyleOptionSpinBox
from PySide6.QtTest import QTest

from yuketang.ai import AIError, CodexClient, DeepSeekClient
from yuketang.config import Config
from yuketang.course import Course, Leaf, CourseAPI
from yuketang.desktop import MainWindow, SettingsDialog, configure_app
from yuketang.desktop_backend import DesktopService, save_settings, settings_snapshot
from yuketang.homework import HomeworkWorker
from yuketang.login import QrcodeLogin, LoginManager
from yuketang.session import YuketangSession
from yuketang.video import VideoWatcher, VideoParams


class SettingsTests(unittest.TestCase):
    def test_packaged_first_launch_does_not_search_parent_directories_for_env(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("yuketang.config.sys.frozen", True, create=True):
                with patch.dict(os.environ, {}, clear=True):
                    with patch("yuketang.config.load_dotenv") as load:
                        config = Config.load(str(Path(directory) / "missing.env"))
        load.assert_not_called()
        self.assertEqual(config.ai_mode, "deepseek")
        self.assertFalse(config.has_credentials)

    def test_roundtrip_preserves_other_backend_and_credentials_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# custom\nSESSION_ID=cookie\nDEEPSEEK_API_KEY=secret\nCODEX_MODEL=old\n", encoding="utf-8")
            config = Config(deepseek_api_key="secret")
            result = save_settings(config, {"ai_mode": "codex", "codex_cli": r"C:\Program Files\Codex\codex.exe",
                                            "codex_model": "custom-model", "skip_completed": False}, path)
            values = dotenv_values(path)
            self.assertEqual(values["SESSION_ID"], "cookie")
            self.assertEqual(values["DEEPSEEK_API_KEY"], "secret")
            self.assertEqual(values["CODEX_CLI"], r"C:\Program Files\Codex\codex.exe")
            self.assertEqual(values["CODEX_MODEL"], "custom-model")
            self.assertEqual(values["SKIP_COMPLETED"], "false")
            self.assertEqual(result.deepseek_api_key, "secret")
            self.assertNotIn("secret", str(settings_snapshot(result)))
            self.assertNotIn("deepseek_api_key", settings_snapshot(result))

    def test_invalid_settings_cannot_partially_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("AI_MODE=deepseek\n", encoding="utf-8")
            for changes in ({"heartbeat_interval": 4}, {"max_concurrent_videos": 0}, {"video_speed": float("nan")},
                            {"codex_cli": "a\nSESSION_ID=bad"}, {"homework_confirm": False},
                            {"deepseek_api_key": "bad"}, {"ai_mode": "auto"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    save_settings(Config(), changes, path)
                self.assertEqual(path.read_text(), "AI_MODE=deepseek\n")

    def test_changing_site_discards_old_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            cfg = save_settings(Config(session_id="cookie", csrf_token="csrf", user_id="42"),
                                {"domain": "other.invalid"}, path)
            self.assertFalse(cfg.has_credentials)
            self.assertEqual(cfg.user_id, "")
            self.assertEqual(dotenv_values(path)["SESSION_ID"], "")

    def test_key_change_and_clear_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            cfg = save_settings(Config(), {}, path, new_key="new-secret")
            self.assertEqual(cfg.deepseek_api_key, "new-secret")
            cfg = save_settings(cfg, {}, path, clear_key=True)
            self.assertFalse(cfg.has_deepseek)
            self.assertEqual(dotenv_values(path)["DEEPSEEK_API_KEY"], "")

    def test_saving_requires_only_selected_backend_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            codex = save_settings(Config(ai_mode="codex", deepseek_base_url="", deepseek_model=""), {}, path)
            self.assertEqual(codex.ai_mode, "codex")
            api = save_settings(Config(codex_cli=""), {}, path)
            self.assertEqual(api.ai_mode, "deepseek")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.service = DesktopService(Config(domain="example.invalid", auto_richtext=False),
                                      lambda *args: self.events.append(args))
        self.service.session = YuketangSession("example.invalid")
        self.service.session.stop_event = self.service.cancel
        self.addCleanup(self.service.session.session.close)
        self.course = Course("offline", 1, "sign", 1, 1)

    def test_open_course_only_requests_chapters_without_task_details_or_ai(self):
        payload = {"data": {"course_chapter": [{"id": 1, "name": "chapter", "section_leaf_list": [
            {"id": 9, "name": "video", "leaf_type": 0}, {"id": 10, "name": "homework", "leaf_type": 6}]}]}}
        with patch.object(self.service.session, "get_json", return_value=payload) as get:
            with patch("yuketang.desktop_backend.create_ai_client") as ai:
                self.service.open_course(self.course)
        get.assert_called_once()
        self.assertIn("/course/chapter", get.call_args.args[0])
        ai.assert_not_called()
        self.assertEqual([leaf.leaf_type for leaf in self.service.leaves], [0, 6])

    def test_running_reuses_loaded_leaves_and_respects_disabled_types(self):
        self.service.course = self.course
        self.service.leaves = [Leaf(1, "video", 0), Leaf(2, "homework", 6), Leaf(3, "discussion", 4)]
        with patch.object(VideoWatcher, "_watch_one", return_value="failed") as watch:
            with patch.object(CourseAPI, "list_leaves") as chapters:
                with patch("yuketang.desktop_backend.create_ai_client") as ai:
                    self.service.run_course()
        watch.assert_called_once()
        chapters.assert_not_called()
        ai.assert_not_called()
        self.assertIn(("task", (1, {"state": "未确认完成", "stage": "", "outcome": "failed"})), self.events)

    def test_stop_during_video_blocks_queued_and_following_tasks(self):
        self.service.course = self.course
        self.service.config.max_concurrent_videos = 1
        self.service.config.auto_richtext = True
        self.service.leaves = [Leaf(i, "video", 0) for i in range(1, 5)] + [Leaf(10, "article", 3)]
        def watch(*args):
            self.service.stop()
            return "skipped"
        with patch.object(VideoWatcher, "_watch_one", side_effect=watch) as worker:
            with patch("yuketang.desktop_backend.RichtextWorker") as rich:
                self.service.run_course()
        worker.assert_called_once()
        rich.assert_not_called()
        self.assertIn(("cancelled", None), self.events)
        self.assertNotIn(("task", (1, {"state": "服务端已完成"})), self.events)

    def test_job_guard_retains_cancellation_until_worker_has_finished(self):
        self.assertTrue(self.service.acquire())
        self.service.stop()
        self.assertFalse(self.service.acquire())
        self.assertTrue(self.service.cancel.is_set())
        self.service.release()
        self.assertTrue(self.service.acquire())
        self.assertFalse(self.service.cancel.is_set())

    def test_homework_cancel_after_ai_does_not_submit_next_answer(self):
        cfg = Config()
        ai = Mock()
        def answer(*args, **kwargs):
            self.service.cancel.set()
            return "A"
        ai.answer.side_effect = answer
        worker = HomeworkWorker(self.service.session, cfg, ai)
        data = {"problems": [{"problem_id": i, "content": {"Type": "SingleChoice", "Body": "offline"}} for i in range(2)]}
        with patch.object(worker, "get_problems", return_value=data):
            with patch.object(worker, "_submit") as submit:
                worker.solve_leaf(self.course, Leaf(8, "homework", 6))
        ai.answer.assert_called_once()
        submit.assert_not_called()

    def test_qr_callback_keeps_login_image_in_memory(self):
        callback = Mock()
        with patch("builtins.print") as output:
            QrcodeLogin("example.invalid", callback=callback)._print_qrcode("synthetic-qr")
        callback.assert_called_once_with("synthetic-qr")
        output.assert_not_called()

    def test_verified_login_sets_session_user_id(self):
        manager = LoginManager(self.service.config)
        with patch.object(self.service.session, "get_json", return_value={"data": {"user_id": 42}}):
            self.assertTrue(manager._verify(self.service.session))
        self.assertEqual(self.service.session.user_id, "42")

    def test_codex_stop_event_terminates_only_launched_process(self):
        event = threading.Event()
        timer = threading.Timer(0.3, event.set)
        started = time.monotonic()
        with patch.object(CodexClient, "_stop_process", wraps=CodexClient._stop_process) as stop:
            timer.start()
            try:
                with self.assertRaisesRegex(AIError, "停止"):
                    CodexClient._run_codex([sys.executable, "-c", "import time; time.sleep(60)"],
                                           input="", timeout=30, stop_event=event, text=True)
            finally:
                timer.cancel()
                timer.join()
        stop.assert_called_once()
        self.assertIsNotNone(stop.call_args.args[0].poll())
        self.assertLess(time.monotonic() - started, 8)

    def test_deepseek_cancel_does_not_retry(self):
        client = DeepSeekClient(Config(deepseek_api_key="synthetic"))
        client.stop_event = self.service.cancel
        client._client = Mock()
        def respond(**kwargs):
            self.service.stop()
            return Mock(choices=[Mock(message=Mock(content="A"))])
        client._client.chat.completions.create.side_effect = respond
        with self.assertRaisesRegex(AIError, "停止"):
            client.chat("", "")
        client._client.chat.completions.create.assert_called_once()


class DesktopUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def setUp(self):
        self.window = MainWindow(demo=True)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def test_demo_load_and_settings_never_access_network_credentials_or_ai(self):
        with patch("yuketang.desktop_backend.Config.load") as load:
            with patch("requests.sessions.Session.request") as network:
                with patch("yuketang.desktop_backend.create_ai_client") as ai:
                    window = MainWindow(demo=True)
                    window.start()
                    window.demo_tick()
                    window.stop()
                    window.close()
        load.assert_not_called()
        network.assert_not_called()
        ai.assert_not_called()

    def test_unknown_is_not_zero_percent_and_local_end_is_not_completion(self):
        item = self.window.items[1]
        self.window.tree.setCurrentItem(item)
        self.assertIn("服务端完成率：未知", item.toolTip(2))
        self.assertNotIn("0.0%", item.toolTip(2))
        self.window.on_event("task", (1, {"position": 300, "duration": 300}))
        self.assertIn("服务端完成率：未知", item.toolTip(2))
        self.assertEqual(item.text(2), "")
        self.assertEqual(self.window.progress_widgets[1][1].value(), 1000)
        self.assertEqual(item.text(1), "未知")

    def test_all_existing_types_and_chapters_remain_navigable(self):
        self.assertEqual(set(self.window.tab_types), {0, 3, 4, 5, 6})
        for index, kind in enumerate(self.window.tab_types):
            self.window.tabs.setCurrentIndex(index)
            expected = {leaf.leaf_id for leaf in self.window.service.leaves if leaf.leaf_type == kind}
            self.assertEqual(set(self.window.items), expected)
        self.window.tabs.setCurrentIndex(0)
        self.assertEqual(self.window.tree.topLevelItemCount(), 2)

    def test_start_stop_prevents_duplicates_and_resets_controls(self):
        QTest.mouseClick(self.window.start_btn, Qt.LeftButton)
        self.assertTrue(self.window.service.busy)
        self.assertFalse(self.window.start_btn.isEnabled())
        self.assertFalse(self.window.settings_btn.isEnabled())
        self.window.demo_tick()
        QTest.mouseClick(self.window.stop_btn, Qt.LeftButton)
        self.assertFalse(self.window.service.busy)
        self.assertFalse(self.window.demo_timer.isActive())
        self.assertEqual(self.window.states[1]["state"], "已停止")
        self.assertTrue(self.window.start_btn.isEnabled())

    def test_settings_mask_key_and_exclude_placeholder_confirmation(self):
        dialog = SettingsDialog(Config(deepseek_api_key="synthetic-secret"), self.window)
        self.assertEqual(dialog.key.text(), "")
        self.assertEqual(dialog.key.echoMode(), QLineEdit.Password)
        self.assertNotIn("homework_confirm", dialog.fields)
        self.assertEqual(dialog.fields["ai_mode"].currentData(), "deepseek")
        dialog.fields["ai_mode"].setCurrentIndex(1)
        self.assertEqual(dialog.ai_pages.currentIndex(), 1)
        self.assertTrue(all(field.text() != "synthetic-secret" for field in dialog.findChildren(QLineEdit)))
        dialog.close()

    def test_settings_controls_and_text_align_across_pages(self):
        dialog = SettingsDialog(Config(), self.window, demo=True)
        dialog.show()
        positions = []
        for tab, key in ((0, "deepseek_model"), (1, "video_speed"), (2, "university_id")):
            dialog.tabs.setCurrentIndex(tab)
            self.app.processEvents()
            field = dialog.fields[key]
            editor = field.lineEdit() if hasattr(field, "lineEdit") else field
            editor.setCursorPosition(0)
            positions.append((field.mapTo(dialog, field.rect().topLeft()).x(),
                              editor.mapTo(dialog, editor.cursorRect().topLeft()).x()))
            form = field.parentWidget().layout()
            caption = form.labelForField(field)
            caption_y = caption.mapTo(dialog, caption.rect().center()).y()
            field_y = field.mapTo(dialog, field.rect().center()).y()
            self.assertLessEqual(abs(caption_y - field_y), 1)
        self.assertEqual(len({x for x, _ in positions}), 1)
        self.assertLessEqual(max(x for _, x in positions) - min(x for _, x in positions), 2)
        self.assertIn("university_id", dialog.school_id_help.text())
        self.assertIn("F12", dialog.school_id_help.text())
        self.assertEqual(dialog.school_id_help.toolTip(), "")
        self.assertNotIn("自动获取", dialog.fields["university_id"].placeholderText())
        dialog.close()

    def test_custom_dropdown_and_spin_arrows_still_operate(self):
        dialog = SettingsDialog(Config(), self.window, demo=True)
        dialog.show()
        self.app.processEvents()
        mode = dialog.fields["ai_mode"]
        mode.showPopup()
        self.app.processEvents()
        position = mode.view().visualRect(mode.model().index(1, 0)).center()
        QTest.mouseClick(mode.view().viewport(), Qt.LeftButton, pos=position)
        self.assertEqual(mode.currentData(), "codex")
        self.assertEqual(dialog.ai_pages.currentIndex(), 1)
        dialog.tabs.setCurrentIndex(1)
        self.app.processEvents()
        spin = dialog.fields["video_speed"]
        option = QStyleOptionSpinBox()
        spin.initStyleOption(option)
        before = spin.value()
        up = spin.style().subControlRect(QStyle.CC_SpinBox, option, QStyle.SC_SpinBoxUp, spin)
        QTest.mouseClick(spin, Qt.LeftButton, pos=up.center())
        self.assertGreater(spin.value(), before)
        down = spin.style().subControlRect(QStyle.CC_SpinBox, option, QStyle.SC_SpinBoxDown, spin)
        QTest.mouseClick(spin, Qt.LeftButton, pos=down.center())
        self.assertEqual(spin.value(), before)
        dialog.close()

    def test_login_home_has_no_intro_and_missing_id_shows_specific_guidance(self):
        window = MainWindow(service=DesktopService(Config()))
        window.show()
        texts = [widget.text() for widget in window.findChildren(QLabel)]
        self.assertNotIn("从这里，开启学习", texts)
        self.assertNotIn("登录后查看你的课程与学习任务", texts)
        self.assertTrue(window.login_hint.isHidden())
        with patch("requests.get") as lookup, patch("websocket.WebSocketApp") as qr:
            window.launch("扫码登录", lambda: window.service.login(True))
            deadline = time.monotonic() + 2
            while window.service.busy and time.monotonic() < deadline:
                time.sleep(0.01)
                self.app.processEvents()
        self.assertIn("手动填写学校 ID", window.status.text())
        lookup.assert_not_called()
        qr.assert_not_called()
        self.assertTrue(window.login_btn.isEnabled())
        window.close()

    def test_switching_course_discards_previous_task_state(self):
        self.window.on_event("task", (1, {"position": 60, "duration": 300, "server_rate": 0.2}))
        self.window.choose_course(self.window.course_list.item(1))
        self.assertEqual(self.window.states, {})
        self.assertEqual(self.window.service.course.classroom_id, 102)

    def test_close_waits_for_actual_worker_then_closes(self):
        self.window.close()
        service = DesktopService(Config())
        window = MainWindow(service=service)
        window.show()
        entered = threading.Event()
        def pending():
            entered.set()
            service.cancel.wait(2)
            time.sleep(0.1)  # outstanding request cleanup
        window.launch("离线等待", pending)
        self.assertTrue(entered.wait(1))
        window.close()
        self.assertTrue(window.isVisible())
        self.assertTrue(service.busy)
        self.assertTrue(service.cancel.is_set())
        deadline = time.monotonic() + 2
        while window.isVisible() and time.monotonic() < deadline:
            time.sleep(0.02)
            self.app.processEvents()
        self.assertFalse(window.isVisible())
        self.assertFalse(service.busy)
        window.close()


from PySide6.QtCore import Qt

if __name__ == "__main__":
    unittest.main()
