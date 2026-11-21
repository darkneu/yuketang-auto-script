"""Clearing school input must preserve authenticated scope, cookies, UI and workers."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dotenv import dotenv_values
from PySide6.QtWidgets import QApplication, QDialog

from yuketang.config import Config
from yuketang.course import Course, Leaf
from yuketang.desktop import MainWindow, SettingsDialog, configure_app
from yuketang.desktop_backend import DesktopService, save_settings, settings_snapshot
from yuketang.homework import HomeworkWorker
from yuketang.login import LoginManager, LoginError, QrcodeLogin
from yuketang.richtext import DiscussionWorker, RichtextWorker
from yuketang.session import YuketangSession
from yuketang.video import VideoWatcher


class SchoolContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.path = self.root / ".env"
        self.enterContext(patch("yuketang.config.PROJECT_ROOT", self.root))
        self.network = self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("real network forbidden")))
        self.config = Config(domain="example.invalid", university_id="123", session_id="offline-session",
                             csrf_token="offline-csrf", user_id="offline-user", cookies={"sessionid": "offline-session"})
        self.path.write_text("UNIVERSITY_ID=123\nSESSION_ID=offline-session\nCSRF_TOKEN=offline-csrf\n# keep this\n", encoding="utf-8")

    def clear(self):
        return save_settings(self.config, {"university_id": "  "}, self.path)

    def test_clear_school_retains_credentials_and_persists_bound_school(self):
        candidate = self.clear()
        self.assertEqual(candidate.university_id, "")
        self.assertEqual(candidate.effective_university_id, "123")
        self.assertEqual(candidate.session_id, self.config.session_id)
        self.assertEqual(candidate.csrf_token, self.config.csrf_token)
        self.assertEqual(candidate.user_id, self.config.user_id)
        self.assertEqual(candidate.cookies, self.config.cookies)
        values = dotenv_values(self.path)
        self.assertEqual(values["UNIVERSITY_ID"], "")
        self.assertEqual(values["SESSION_UNIVERSITY_ID"], "123")
        self.assertEqual(values["SESSION_ID"], "offline-session")
        self.assertEqual(values["CSRF_TOKEN"], "offline-csrf")
        self.assertIn("# keep this", self.path.read_text())
        self.assertNotIn("session_university_id", settings_snapshot(candidate))

    def test_reload_and_repeat_save_keep_login_scope_with_blank_input(self):
        self.clear()
        with patch.dict(os.environ, {}, clear=True):
            reloaded = Config.load(str(self.path))
        self.assertEqual(reloaded.university_id, "")
        self.assertEqual(reloaded.effective_university_id, "123")
        self.assertEqual(reloaded.build_cookies()["university_id"], "123")
        again = save_settings(reloaded, {"university_id": "", "video_speed": 2}, self.path)
        self.assertEqual(again.session_id, "offline-session")
        self.assertEqual(again.effective_university_id, "123")

    def test_reenter_same_school_retains_login(self):
        cfg = self.clear()
        cfg = save_settings(cfg, {"university_id": "123"}, self.path)
        self.assertTrue(cfg.has_credentials)
        self.assertEqual(cfg.user_id, "offline-user")

    def test_different_school_or_domain_still_discards_old_session(self):
        cleared = self.clear()
        for changes in ({"university_id": "456"}, {"domain": "other.invalid"},
                        {"domain": "other.invalid", "university_id": ""}):
            with self.subTest(changes=changes):
                cfg = save_settings(cleared, changes, self.path)
                self.assertFalse(cfg.has_credentials)
                self.assertEqual(cfg.csrf_token, "")
                self.assertEqual(cfg.user_id, "")
                self.assertFalse(cfg.cookies)
                self.assertEqual(cfg.session_university_id, "")
                self.assertEqual(dotenv_values(self.path)["SESSION_UNIVERSITY_ID"], "")

    def test_bound_school_without_credentials_cannot_supply_login_scope(self):
        cfg = Config(session_university_id="123")
        self.assertEqual(cfg.effective_university_id, "")
        with patch.object(QrcodeLogin, "run") as qr:
            with self.assertRaisesRegex(LoginError, "手动填写学校 ID"):
                LoginManager(cfg).login()
        qr.assert_not_called()
        self.network.assert_not_called()

    def test_saved_cookie_verification_uses_bound_school_without_qr_or_lookup(self):
        cfg = self.clear()
        seen = []
        def verify(session):
            seen.append((session.university_id, session.headers["University-Id"], session.session.cookies.get("university_id")))
            return True
        with patch.object(LoginManager, "_verify", side_effect=verify), patch.object(QrcodeLogin, "run") as qr:
            session = LoginManager(cfg).login()
        self.addCleanup(session.session.close)
        self.assertEqual(seen, [("123", "123", "123")])
        qr.assert_not_called()
        self.network.assert_not_called()

    def test_verified_login_remembers_school_without_repopulating_manual_input(self):
        cfg = self.clear()
        manager = LoginManager(cfg)
        session = manager._build_session(cfg.build_cookies())
        self.addCleanup(session.session.close)
        with patch.object(session, "get_json", return_value={"success": True, "user_id": 42}):
            self.assertTrue(manager._verify(session))
        self.assertEqual(cfg.session_university_id, "123")
        self.assertEqual(cfg.university_id, "")

    def test_logout_clears_binding_and_routes_blank_school_back_to_setup(self):
        service = DesktopService(self.clear())
        service.session = YuketangSession("example.invalid", university_id="123")
        service.logout()
        self.assertEqual(service.config.effective_university_id, "")
        self.assertEqual(service.config.session_university_id, "")
        self.assertEqual(dotenv_values(self.path)["SESSION_UNIVERSITY_ID"], "")

    def test_business_requests_keep_same_school_after_input_is_cleared(self):
        cfg = self.clear()
        session = YuketangSession("example.invalid", university_id="123", user_id="42")
        self.addCleanup(session.session.close)
        course = Course("offline", 1, "", 2, 3)
        worker = HomeworkWorker(session, cfg)
        for kind in (5, 6):
            with self.subTest(kind=kind), patch.object(session, "get_json", side_effect=[
                {"success": True, "data": {"sku_id": 2, "content_info": {"leaf_type_id": 3}}},
                {"success": True, "data": {"problems": [{"problem_id": 4, "user": {"my_count": 1, "count": 1}}]}},
            ]) as get, patch.object(worker, "_solve") as solve:
                result = worker.solve_leaf(course, Leaf(4, "offline", kind))
                self.assertIn("uv_id=123", get.call_args_list[1].args[0])
                self.assertEqual(result["skipped"], 1)
                solve.assert_not_called()
        with patch.object(session, "post_json", return_value={"success": True}) as post:
            worker._submit(course, 4, "offline", "ShortAnswer")
            self.assertIn("uv_id=123", post.call_args.args[0])
        self.assertEqual(RichtextWorker(session, cfg).course_api.university_id, "123")
        watcher = VideoWatcher(session, cfg)
        with patch.object(session, "get_json", side_effect=[
            {"success": True, "data": {"user_id": 42, "content_info": {"media": {"ccid": "offline"}}}},
            {"success": True, "data": {"heartbeat": {"video_length": 100, "rate": 1, "completed": 1}}},
        ]) as get:
            params = watcher.build_params(course, Leaf(5, "video", 0))
            self.assertEqual(params.university_id, "123")
            self.assertIn("uv_id=123", get.call_args.args[0])
        ai = Mock()
        ai.answer_discussion.return_value = "offline discussion"
        discussion = DiscussionWorker(session, cfg, ai)
        with patch.object(discussion, "_get_topic", return_value={"text": "offline", "sku_id": 2}), \
             patch.object(discussion, "_get_discussion_target", return_value={"topic_id": 3, "to_user": 4}), \
             patch.object(session, "post_json", return_value={"success": True}) as post:
            self.assertTrue(discussion.post_one(course, Leaf(6, "discussion", 4)))
            self.assertIn("uv_id=123", post.call_args.args[0])


class SchoolContextUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.enterContext(patch("yuketang.config.PROJECT_ROOT", self.root))
        self.network = self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("real network forbidden")))
        config = Config(domain="example.invalid", university_id="123", session_id="offline-session", csrf_token="offline-csrf")
        (self.root / ".env").write_text("UNIVERSITY_ID=123\nSESSION_ID=offline-session\nCSRF_TOKEN=offline-csrf\n", encoding="utf-8")
        self.service = DesktopService(config)
        self.session = YuketangSession("example.invalid", university_id="123", user_id="42")
        self.service.session = self.session
        self.window = MainWindow(service=self.service)
        self.addCleanup(self.window.close)
        self.course = Course("offline", 1, "", 1, 1)
        self.window.on_event("courses", [self.course])
        self.window.on_event("leaves", (self.course, [Leaf(1, "video", 0)]))
        self.window.set_busy(False)
        self.window.on_event("task", (1, {"server_rate": 1.0, "server_completed": True, "state": "服务端已完成", "outcome": "success"}))
        self.window.flush_task_states()

    def save_dialog(self, values):
        def save(dialog):
            for key, value in values.items():
                dialog.fields[key].setText(value)
            dialog.save()
            return dialog.result()
        with patch.object(SettingsDialog, "exec", save):
            self.window.open_settings(2)

    def test_clear_input_keeps_session_courses_cache_and_task_controls(self):
        old_cache_path = self.window.task_state_store.path
        with patch.object(self.session.session, "close", wraps=self.session.session.close) as close:
            self.save_dialog({"university_id": ""})
            close.assert_not_called()
        self.assertIs(self.service.session, self.session)
        self.assertEqual(self.service.config.university_id, "")
        self.assertEqual(self.service.config.effective_university_id, "123")
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertIs(self.service.course, self.course)
        self.assertEqual(self.window.course_list.count(), 1)
        self.assertEqual(self.window.states[1]["state"], "服务端已完成")
        self.assertEqual(self.window.task_state_store.path, old_cache_path)
        self.assertTrue(self.window.start_btn.isEnabled())
        self.assertTrue(self.window.refresh_btn.isEnabled())
        self.assertTrue(self.window.account_btn.isEnabled())
        dialog = SettingsDialog(self.service.config, self.window)
        self.assertEqual(dialog.fields["university_id"].text(), "")
        dialog.close()
        self.app.processEvents()
        self.network.assert_not_called()

    def test_switch_to_another_school_still_clears_old_views_and_login(self):
        self.save_dialog({"university_id": "456"})
        self.assertIsNone(self.service.session)
        self.assertFalse(self.service.config.has_credentials)
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.assertEqual(self.window.course_list.count(), 0)
        self.assertFalse(self.window.states)
        self.assertIsNone(self.window.task_state_store.path)
        self.assertFalse(self.window.start_btn.isEnabled())

    def test_restart_uses_saved_login_context_with_blank_school_input(self):
        self.save_dialog({"university_id": ""})
        self.window.close()
        with patch.dict(os.environ, {}, clear=True):
            reloaded = Config.load(str(self.root / ".env"))
        service = DesktopService(reloaded)
        with patch.object(service, "login") as login:
            window = MainWindow(service=service)
            self.addCleanup(window.close)
            for _ in range(12):
                self.app.processEvents()
            if window.job:
                window.job.join(2)
                self.app.processEvents()
        login.assert_called_once_with()
        self.assertEqual(reloaded.university_id, "")
        self.assertEqual(reloaded.effective_university_id, "123")
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
