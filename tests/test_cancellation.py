"""Cancellation tests without any requests to the course platform."""

import _thread
import threading
import time
import unittest
from unittest.mock import patch

import requests

from yuketang.config import Config
from yuketang.course import Course, Leaf
from yuketang.session import YuketangSession
from yuketang.video import VideoWatcher


class CancellationTests(unittest.TestCase):
    def test_parallel_video_interrupt_stops_workers_and_pending_tasks(self):
        session = YuketangSession("example.invalid")
        self.addCleanup(session.session.close)
        watcher = VideoWatcher(session, Config(max_concurrent_videos=2))
        course = Course("test", 1, "test", 1, 1)
        leaves = [Leaf(i, str(i), 0) for i in range(6)]
        started, finished = [], []

        def work(course, leaf, *args):
            if session.stop_event.is_set():
                return "skipped"
            started.append(leaf.leaf_id)
            try:
                watcher._pause(60)
            finally:
                finished.append(leaf.leaf_id)

        timer = threading.Timer(0.5, _thread.interrupt_main)
        start = time.monotonic()
        with patch.object(watcher.course_api, "list_leaves", return_value=leaves):
            with patch.object(watcher, "_watch_one", side_effect=work):
                timer.start()
                try:
                    with self.assertRaises(KeyboardInterrupt):
                        watcher.watch_course(course)
                finally:
                    timer.cancel()
                    timer.join()
        self.assertTrue(session.stop_event.is_set())
        self.assertEqual(len(started), 2)
        self.assertCountEqual(finished, started)
        self.assertLess(time.monotonic() - start, 3)

    def test_cancelled_session_sends_no_new_request(self):
        session = YuketangSession("example.invalid")
        self.addCleanup(session.session.close)
        session.stop_event.set()
        with patch.object(session.session, "request") as send:
            self.assertIsNone(session.get("/test"))
        send.assert_not_called()

    def test_cancel_during_backoff_prevents_retry(self):
        session = YuketangSession("example.invalid", max_retries=3)
        self.addCleanup(session.session.close)
        timer = threading.Timer(0.2, session.stop_event.set)
        with patch.object(session.session, "request", side_effect=requests.exceptions.Timeout) as send:
            timer.start()
            try:
                self.assertIsNone(session.get("/test"))
            finally:
                timer.cancel()
                timer.join()
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
