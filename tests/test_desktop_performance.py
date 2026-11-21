"""Request parity, skip paths and UI backpressure regressions; entirely offline."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from yuketang.config import Config
from yuketang.course import Course, Leaf
from yuketang.desktop import MainWindow, configure_app
from yuketang.desktop_backend import DesktopService
from yuketang.desktop_runtime import _EventRelay
from yuketang.homework import HomeworkWorker
from yuketang.richtext import DiscussionWorker, RichtextWorker
from yuketang.session import YuketangSession
from yuketang.video import VideoWatcher


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.session = YuketangSession("example.invalid")
        self.addCleanup(self.session.session.close)
        self.course = Course("offline", 1, "", 1, 1)
        self.config = Config(auto_video=True, auto_richtext=True, auto_discussion=True,
                             auto_homework=True, auto_exam=True, deepseek_api_key="offline")

    @staticmethod
    def problem(pid, *, completed=False):
        return {"problem_id": pid, "content": {"Type": "ShortAnswer", "Body": "<p>body</p>"},
                "user": {"my_count": 1, "count": 1} if completed else {}}

    def test_all_task_types_have_cli_desktop_request_parity_when_completed(self):
        def response(url, **kwargs):
            if "leaf_info" in url:
                return {"success": True, "data": {"finish": True, "user_id": 1, "sku_id": 1,
                    "content_info": {"leaf_type_id": 1, "context": "<p>topic</p>",
                                     "media": {"ccid": "offline", "duration": 100}}}}
            if "video-log/detail" in url:
                return {"success": True, "data": {"heartbeat": {"video_length": 100, "rate": 1, "completed": 1}}}
            if "user_article_finish_status" in url:
                return {"success": True, "data": {"finish": 1}}
            if "get_exercise_list" in url:
                return {"success": True, "data": {"font": "https://example.invalid/font",
                    "problems": [self.problem(1, completed=True)]}}
            self.fail("Unexpected request: " + url)

        for kind, expected in ((0, 2), (3, 1), (4, 1), (5, 2), (6, 2)):
            with self.subTest(kind=kind):
                requests = []
                for desktop in (False, True):
                    self.session.observer = Mock() if desktop else None
                    leaf = Leaf(kind + 10, "completed", kind)
                    ai = Mock()
                    with patch.object(self.session, "get_json", side_effect=response) as get, \
                         patch.object(self.session, "post_json") as post, \
                         patch("yuketang.homework.FontDecryptor.load_from_url") as font, \
                         patch.object(HomeworkWorker, "_parse_problem") as parse, \
                         patch.object(VideoWatcher, "_duration_from_play_url") as duration:
                        if desktop:
                            service = DesktopService(self.config)
                            service.session, service.course, service.leaves = self.session, self.course, [leaf]
                            with patch("yuketang.desktop_backend.create_ai_client", return_value=ai):
                                service.run_course()
                        elif kind == 0:
                            VideoWatcher(self.session, self.config)._watch_one(self.course, leaf, None, None, True, None)
                        elif kind == 3:
                            RichtextWorker(self.session, self.config).finish_one(self.course, leaf)
                        elif kind == 4:
                            DiscussionWorker(self.session, self.config, ai).post_one(self.course, leaf)
                        else:
                            HomeworkWorker(self.session, self.config, ai).solve_leaf(self.course, leaf)
                        self.assertEqual(get.call_count, expected)
                        requests.append(get.call_args_list)
                        post.assert_not_called()
                        font.assert_not_called()
                        parse.assert_not_called()
                        duration.assert_not_called()
                        ai.answer.assert_not_called()
                        ai.answer_discussion.assert_not_called()
                self.assertEqual(requests[0], requests[1])

    def test_locked_exam_does_not_request_any_details(self):
        with patch.object(self.session, "get_json") as get:
            result = HomeworkWorker(self.session, self.config).solve_leaf(
                self.course, Leaf(1, "locked", 5, raw={"is_locked": True}))
        get.assert_not_called()
        self.assertEqual(result["total"], 0)

    def test_initial_video_snapshot_is_not_reused_after_playback(self):
        watcher = VideoWatcher(self.session, self.config)
        partial = {"success": True, "data": {"heartbeat": {"video_length": 100, "rate": 0.5, "completed": 0}}}
        complete = {"success": True, "data": {"heartbeat": {"video_length": 100, "rate": 1, "completed": 1}}}
        info = {"success": True, "data": {"user_id": 1, "content_info": {"media": {"ccid": "offline"}}}}
        def play(params, **kwargs):
            self.assertEqual(kwargs["initial_progress"]["rate"], 0.5)
            self.assertIsNone(params.initial_progress)
            return watcher._is_complete(watcher.get_progress(params))
        with patch.object(self.session, "get_json", side_effect=[info, partial, complete]) as get, \
             patch.object(watcher, "watch", side_effect=play):
            result = watcher._watch_one(self.course, Leaf(1, "video", 0), None, None, True, None)
        self.assertEqual(result, "success")
        self.assertEqual(get.call_count, 3)

    def test_cli_completed_course_fetches_chapters_once_and_does_not_wait(self):
        worker = HomeworkWorker(self.session, self.config)
        leaves = [Leaf(1, "exam", 5), Leaf(2, "homework", 6)]
        order = []
        def solve(course, leaf):
            order.append(leaf.leaf_type)
            return {"total": 1, "success": 1, "failed": 0, "skipped": 1}
        with patch.object(worker.course_api, "list_leaves", return_value=leaves) as chapters, \
             patch.object(worker, "solve_leaf", side_effect=solve), \
             patch.object(self.session.stop_event, "wait", side_effect=AssertionError("wait")):
            result = worker.solve_course(self.course, include_exam=True)
        chapters.assert_called_once_with(self.course)
        self.assertEqual(order, [6, 5])
        self.assertEqual(result["success"], 2)

    def test_completed_homework_never_loads_slow_font_parses_or_waits(self):
        worker = HomeworkWorker(self.session, self.config, Mock())
        payload = {"font": "https://example.invalid/slow-font", "problems": [self.problem(i, completed=True) for i in range(100)]}
        with patch.object(worker, "get_problems", return_value=payload), \
             patch.object(worker.decryptor, "load_from_url", side_effect=AssertionError("font")), \
             patch.object(worker, "_parse_problem", side_effect=AssertionError("parse")), \
             patch.object(self.session.stop_event, "wait", side_effect=AssertionError("wait")), \
             patch.object(worker, "_submit") as submit:
            result = worker.solve_leaf(self.course, Leaf(1, "done", 6))
        self.assertEqual(result["skipped"], 100)
        submit.assert_not_called()

    def test_course_worker_reuses_font_and_parses_only_pending_problems(self):
        service = DesktopService(self.config)
        service.session, service.course = self.session, self.course
        service.leaves = [Leaf(1, "homework", 6), Leaf(2, "exam", 5)]
        payload = {"font": "https://example.invalid/shared-font", "problems": [
            self.problem(1, completed=True), self.problem(2)]}
        events = []
        self.session.observer = lambda _, values: events.append(values)
        ai = Mock()
        ai.answer.return_value = "offline answer"
        parse = HomeworkWorker._parse_problem
        parsed = []
        def parse_once(worker, problem):
            parsed.append(problem["problem_id"])
            return parse(worker, problem)
        with patch.object(HomeworkWorker, "get_problems", return_value=payload), \
             patch("yuketang.desktop_backend.create_ai_client", return_value=ai), \
             patch("yuketang.homework.FontDecryptor.load_from_url", return_value=True) as font, \
             patch.object(HomeworkWorker, "_parse_problem", parse_once), \
             patch.object(HomeworkWorker, "_submit", return_value=True) as submit, \
             patch.object(self.session.stop_event, "wait", return_value=False):
            service.run_course()
        font.assert_called_once()
        self.assertEqual(parsed, [2, 2])
        self.assertEqual(ai.answer.call_count, 2)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(sum("body" in value.get("problem", {}) for value in events), 2)

    def test_changed_failed_or_missing_font_never_reuses_previous_mapping(self):
        worker = HomeworkWorker(self.session, self.config)
        worker._loaded_font_url = "old"
        worker.decryptor.char_map = {"a": "wrong"}
        worker.decryptor._loaded = True
        with patch.object(worker.decryptor, "load_from_url", return_value=False):
            worker._prepare_font("new")
        self.assertFalse(worker.decryptor.loaded)
        self.assertFalse(worker.decryptor.char_map)
        worker.decryptor.char_map = {"a": "wrong"}
        worker.decryptor._loaded = True
        worker._loaded_font_url = "old"
        worker._prepare_font(None)
        self.assertFalse(worker.decryptor.loaded)

    def test_unknown_or_partial_homework_counts_still_require_solving(self):
        worker = HomeworkWorker(self.session, self.config, Mock())
        problem = self.problem(1)
        for user in ({}, {"my_count": 0, "count": 1}, {"my_count": 1, "count": 2}, {"my_count": 1}):
            problem["user"] = user
            with patch.object(worker, "get_problems", return_value={"problems": [problem]}), \
                 patch.object(worker, "_solve", return_value=None) as solve:
                result = worker.solve_leaf(self.course, Leaf(1, "pending", 6))
            solve.assert_called_once()
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["skipped"], 0)


class RelayTests(unittest.TestCase):
    def test_pipe_backpressure_cannot_block_business_and_order_is_preserved(self):
        entered, release = threading.Event(), threading.Event()
        sent = []
        sender = Mock()
        def send(value):
            entered.set()
            release.wait(2)
            sent.append(value)
        sender.send.side_effect = send
        relay = _EventRelay(sender)
        try:
            relay.emit("task", 0)
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            for i in range(1, 100):
                relay.emit("task", i)
            relay.emit("run_done", 100)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertFalse(sent)
        finally:
            release.set()
            relay.close()
        self.assertEqual(sent, [("task", i) for i in range(100)] + [("run_done", 100)])
        sender.close.assert_called_once()


class IncrementalUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def setUp(self):
        self.window = MainWindow(demo=True)
        self.window.service.config.auto_homework = True
        self.window.start()
        self.window.demo_timer.stop()
        self.addCleanup(self.window.close)
        self.addCleanup(self.window.stop)

    def test_answer_delta_keeps_body_and_does_not_rewrite_prior_questions(self):
        for i in range(40):
            self.window.on_event("task", (6, {"state": "处理中", "problem": {
                "id": i, "type": "ShortAnswer", "body": f"question {i}", "state": "生成答案中"}}))
        card = self.window.run_panel.cards[6]
        with patch.object(card.problems[0][0], "setText") as title, \
             patch.object(card.problems[0][1], "setText") as body, \
             patch.object(card.problems[0][2], "setText") as old_answer:
            self.window.on_event("task", (6, {"problem": {"id": 39, "state": "提交中", "answer": "A"}}))
            self.window.on_event("task", (6, {"problem": {"id": 39, "state": "已提交"}}))
        title.assert_not_called()
        body.assert_not_called()
        old_answer.assert_not_called()
        self.assertEqual(card.problems[39][1].text(), "question 39")
        self.assertIn("A", card.problems[39][2].text())
        self.assertIn("第 40 题", card.problems[39][0].text())

    def test_question_is_visible_while_ai_waits_and_cancellation_blocks_submit(self):
        session = YuketangSession("example.invalid")
        self.addCleanup(session.session.close)
        session.observer = lambda leaf_id, values: self.window.bridge.event.emit("task", (leaf_id, values))
        entered, release = threading.Event(), threading.Event()
        ai = Mock()
        def answer(*args, **kwargs):
            entered.set()
            release.wait(3)
            return "A"
        ai.answer.side_effect = answer
        worker = HomeworkWorker(session, Config(), ai)
        self.window.on_event("task", (6, {"state": "处理中"}))
        with patch.object(worker, "get_problems", return_value={"problems": [RequestTests.problem(1)]}), \
             patch.object(worker, "_submit") as submit:
            job = threading.Thread(target=worker.solve_leaf, args=(Course("offline", 1, "", 1, 1), Leaf(6, "offline", 6)))
            job.start()
            try:
                self.assertTrue(entered.wait(1))
                for _ in range(5):
                    self.app.processEvents()
                self.assertTrue(job.is_alive())
                self.assertEqual(self.window.run_panel.cards[6].problems[1][1].text(), "body")
                session.stop_event.set()
            finally:
                release.set()
                job.join(2)
            self.assertFalse(job.is_alive())
            submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
