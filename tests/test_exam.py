"""Offline exam scheduling, response handling and submission boundaries."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from yuketang.config import Config
from yuketang.course import Course, Leaf, LEAF_EXAM, LEAF_HOMEWORK
from yuketang.desktop import MainWindow, configure_app
from yuketang.desktop_backend import DesktopService
from yuketang.homework import HomeworkWorker
from yuketang.session import YuketangSession


class ExamTests(unittest.TestCase):
    def setUp(self):
        # Even a missing mock must never reach the network.
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("real network forbidden"))
        self.enterContext(network)
        self.session = YuketangSession("example.invalid")
        self.addCleanup(self.session.session.close)
        self.config = Config(auto_video=False, auto_richtext=False, auto_discussion=False,
                             auto_homework=False, auto_exam=True, deepseek_api_key="offline")
        self.course = Course("offline", 1, "", 2, 3)
        self.leaf = Leaf(10, "offline exam", LEAF_EXAM, sku_id=7, leafinfo_id=8)
        self.ai = Mock()
        self.ai.answer.return_value = "offline answer"
        self.events = []
        self.service = DesktopService(self.config, lambda *event: self.events.append(event))
        self.service.session, self.service.course = self.session, self.course
        self.session.stop_event = self.service.cancel
        self.session.observer = lambda lid, values: self.service.emit("task", (lid, values))
        self.worker = HomeworkWorker(self.session, self.config, self.ai)
        self.get = self.enterContext(patch.object(self.session, "get_json"))
        self.post = self.enterContext(patch.object(self.session, "post_json", return_value={"success": True}))
        wait = patch.object(self.session.stop_event, "wait", return_value=False)
        self.enterContext(wait)

    @staticmethod
    def info(**values):
        return {"success": True, "data": {"sku_id": 20, "content_info": {"leaf_type_id": 30}, **values}}

    @staticmethod
    def exercise(**values):
        return {"success": True, "data": {"problems": [
            {"problem_id": 40, "content": {"Type": "ShortAnswer", "Body": "<p>offline question</p>"}}
        ], **values}}

    def result_event(self, lid=10):
        return [payload[1] for kind, payload in self.events
                if kind == "task" and payload[0] == lid and "outcome" in payload[1]][-1]

    def run_service(self, leaves=None):
        self.service.leaves = leaves if leaves is not None else [self.leaf]
        with patch("yuketang.desktop_backend.create_ai_client", return_value=self.ai):
            self.service.run_course()

    def test_exam_only_switch_schedules_and_submits_using_detail_ids(self):
        self.service.leaves = [Leaf(9, "disabled homework", LEAF_HOMEWORK), self.leaf]
        self.assertEqual(self.service.selected_leaves(), [self.leaf])
        self.get.side_effect = [self.info(), self.exercise()]
        self.run_service(self.service.leaves)
        self.assertEqual(self.get.call_count, 2)
        self.assertIn("/leaf_info/1/10/", self.get.call_args_list[0].args[0])
        self.assertIn("/get_exercise_list/30/20/", self.get.call_args_list[1].args[0])
        self.post.assert_called_once()
        self.assertIn("/problem_apply/", self.post.call_args.args[0])
        self.assertEqual(self.post.call_args.kwargs["payload"],
                         {"classroom_id": 1, "problem_id": 40, "answer": "offline answer"})
        self.assertEqual(self.result_event()["outcome"], "success")
        self.assertIn(("run_done", 1), self.events)

    def test_false_encodings_do_not_skip_open_exam(self):
        for value in (False, 0, "false", "False", " FALSE ", "0"):
            with self.subTest(value=value):
                self.leaf.raw = {"is_locked": value, "start_time": "1000000000000"}
                self.get.side_effect = [self.info(is_locked=value), self.exercise(is_locked=value)]
                self.get.reset_mock()
                result = self.worker.solve_leaf(self.course, self.leaf)
                self.assertEqual(result["success"], 1)
                self.assertEqual(self.get.call_count, 2)

    def test_real_lock_including_string_start_time_never_fetches_or_submits(self):
        for value in (True, 1, "true", " TRUE ", "1"):
            with self.subTest(value=value):
                self.leaf.raw = {"is_locked": value, "start_time": "1800000000000"}
                result = self.worker.solve_leaf(self.course, self.leaf)
                self.assertEqual(result["status"], "locked")
                self.assertEqual(result["success"], 0)
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.ai.answer.assert_not_called()

    def test_unknown_lock_and_invalid_times_fail_closed(self):
        for raw in ({"is_locked": "unknown"}, {"is_locked": 2}, {"is_locked": []},
                    {"start_time": "invalid"}, {"start_time": True}, {"end_time": "nan"}):
            with self.subTest(raw=raw):
                self.leaf.raw = raw
                self.assertEqual(self.worker.solve_leaf(self.course, self.leaf)["status"], "access_unknown")
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_unopened_and_expired_exam_do_not_fetch(self):
        for field, stamp, expected in (("start_time", 2000, "not_open"), ("end_time", 900, "closed")):
            for scale in (1, 1000):
                with self.subTest(field=field, scale=scale), patch("yuketang.homework.time.time", return_value=1_800_000_000):
                    # Use contemporary epochs so seconds and milliseconds remain unambiguous.
                    epoch = 1_800_000_000 + stamp - 1000
                    self.leaf.raw = {"is_locked": False, field: str(epoch * scale)}
                    self.assertEqual(self.worker.solve_leaf(self.course, self.leaf)["status"], expected)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_detail_and_content_locks_block_question_request(self):
        for info in (self.info(is_locked=True), self.info(content_info={"leaf_type_id": 30, "is_locked": "1"}),
                     self.info(start_time="9999999999999")):
            with self.subTest(info=info):
                self.get.reset_mock()
                self.get.return_value = info
                result = self.worker.solve_leaf(self.course, self.leaf)
                self.assertIn(result["status"], {"locked", "not_open"})
                self.get.assert_called_once()
        self.ai.answer.assert_not_called()
        self.post.assert_not_called()

    def test_failed_detail_does_not_fall_back_to_old_leaf_ids(self):
        for response in (None, {"success": False}, {"success": True, "data": []}):
            with self.subTest(response=response):
                self.get.reset_mock()
                self.get.return_value = response
                result = self.worker.solve_leaf(self.course, self.leaf)
                self.assertEqual(result["status"], "fetch_failed")
                self.get.assert_called_once()
        self.post.assert_not_called()

    def test_successful_detail_keeps_existing_id_fallback(self):
        self.get.side_effect = [{"success": True, "data": {}}, self.exercise()]
        result = self.worker.solve_leaf(self.course, self.leaf)
        self.assertEqual(result["success"], 1)
        self.assertIn("/get_exercise_list/8/7/", self.get.call_args_list[1].args[0])

    def test_missing_ids_and_malformed_detail_are_reported(self):
        for response, expected in ((self.info(content_info={}), "missing_ids"),
                                   (self.info(content_info=[]), "invalid_data"),
                                   (self.info(content_info="bad"), "invalid_data")):
            with self.subTest(expected=expected):
                self.leaf.leafinfo_id = 0
                self.get.reset_mock()
                self.get.return_value = response
                self.assertEqual(self.worker.solve_leaf(self.course, self.leaf)["status"], expected)
                self.get.assert_called_once()
        self.post.assert_not_called()

    def test_exercise_denial_malformed_or_empty_payload_is_never_success(self):
        for response, expected in ((None, "题目获取失败"), ({"success": False, "msg": "offline denial"}, "题目获取失败"),
                                   ({"success": True, "data": {}}, "接口数据异常"),
                                   (self.exercise(problems={}), "接口数据异常"),
                                   (self.exercise(problems=[]), "暂无题目"),
                                   (self.exercise(is_locked=True), "考试已锁定")):
            with self.subTest(expected=expected):
                self.get.side_effect = [self.info(), response]
                self.events.clear()
                self.run_service()
                result = self.result_event()
                self.assertEqual(result["state"], expected)
                self.assertNotEqual(result["outcome"], "success")
                self.assertTrue(result["reason"])
        self.ai.answer.assert_not_called()
        self.post.assert_not_called()

    def test_cancel_during_detail_prevents_question_request(self):
        def response(*args, **kwargs):
            self.service.stop()
            return self.info()
        self.get.side_effect = response
        self.run_service()
        self.get.assert_called_once()
        self.post.assert_not_called()
        self.assertEqual(self.result_event()["outcome"], "stopped")
        self.assertIn(("cancelled", None), self.events)
        self.assertNotIn(("run_done", 1), self.events)

    def test_cancel_after_answer_prevents_submit(self):
        self.get.side_effect = [self.info(), self.exercise()]
        def answer(*args, **kwargs):
            self.service.stop()
            return "offline answer"
        self.ai.answer.side_effect = answer
        self.run_service()
        self.post.assert_not_called()
        self.assertEqual(self.result_event()["outcome"], "stopped")

    def test_deadline_crossed_during_ai_prevents_submit(self):
        now = [1_800_000_000]
        self.get.side_effect = [self.info(end_time="1800000001000"), self.exercise()]
        def answer(*args, **kwargs):
            now[0] += 2
            return "offline answer"
        self.ai.answer.side_effect = answer
        with patch("yuketang.homework.time.time", side_effect=lambda: now[0]):
            self.run_service()
        self.post.assert_not_called()
        self.assertEqual(self.result_event()["state"], "考试已截止")
        self.assertEqual(self.result_event()["outcome"], "unprocessed")

    def test_submit_and_generation_failure_are_not_success(self):
        for fail_ai in (True, False):
            with self.subTest(fail_ai=fail_ai):
                self.get.side_effect = [self.info(), self.exercise()]
                self.ai.answer.return_value = None if fail_ai else "offline answer"
                self.post.return_value = {"success": False}
                self.events.clear()
                self.run_service()
                self.assertEqual(self.result_event()["state"], "部分失败")
                self.assertEqual(self.result_event()["outcome"], "failed")

    def test_incomplete_counts_cannot_be_promoted_to_success(self):
        with patch.object(HomeworkWorker, "solve_leaf", return_value={"total": 2, "success": 1, "failed": 0}):
            self.run_service()
        self.assertEqual(self.result_event()["state"], "未完成")
        self.assertEqual(self.result_event()["outcome"], "unprocessed")

    def test_one_failed_exam_does_not_hide_error_or_abort_next_exam(self):
        # A malformed problem reproduces the broad desktop exception handler.
        self.get.side_effect = [self.info(), self.exercise(problems=[None]), self.info(), self.exercise()]
        self.run_service([self.leaf, Leaf(11, "next exam", LEAF_EXAM)])
        self.assertEqual(self.result_event()["outcome"], "failed")
        self.assertIn("AttributeError", self.result_event()["reason"])
        self.assertEqual(self.result_event(11)["outcome"], "success")
        self.assertEqual(self.get.call_count, 4)
        self.post.assert_called_once()

    def test_explicit_locks_are_visible_unprocessed_in_course_run(self):
        self.leaf.raw = {"is_locked": True, "start_time": "1800000000000"}
        self.run_service()
        self.assertEqual(self.result_event()["state"], "考试已锁定")
        self.assertEqual(self.result_event()["outcome"], "unprocessed")
        self.assertIn("锁定", self.result_event()["reason"])
        self.get.assert_not_called()
        self.post.assert_not_called()


class ExamUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def test_title_and_exam_reason_are_visible_without_credentials(self):
        window = MainWindow(demo=True)
        self.addCleanup(window.close)
        self.assertEqual(window.windowTitle(), "雨课堂  [演示数据]")
        self.assertEqual(self.app.applicationName(), "雨课堂")
        window.service.config.auto_exam = True
        window.start()
        window.demo_timer.stop()
        self.addCleanup(window.stop)
        reason = "考试已锁定或尚未开放，请在平台确认开放状态"
        window.on_event("task", (7, {"state": "考试已锁定", "outcome": "unprocessed", "reason": reason}))
        card = window.run_panel.cards[7]
        self.assertEqual(card.reason.text(), reason)
        self.assertFalse(card.reason.isHidden())
        self.assertIn("已完成 0", window.run_panel.stats.text())
        window.tabs.setCurrentIndex(window.tab_types.index(LEAF_EXAM))
        self.assertIn(reason, window.items[7].toolTip(1))
        plain = MainWindow(service=DesktopService(Config()))
        self.addCleanup(plain.close)
        self.assertEqual(plain.windowTitle(), "雨课堂")


if __name__ == "__main__":
    unittest.main()
