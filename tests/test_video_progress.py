"""Video regressions based on the observed partial coverage; no live writes."""

import argparse
import unittest
from unittest.mock import patch

from yuketang.config import Config
from yuketang.course import Course, Leaf
from yuketang.session import YuketangSession
from yuketang.video import VideoParams, VideoWatcher
from yuketang.cli import cmd_all, cmd_video


class VideoProgressTests(unittest.TestCase):
    def setUp(self):
        self.session = YuketangSession("example.invalid")
        self.addCleanup(self.session.session.close)
        self.watcher = VideoWatcher(self.session, Config())
        self.params = VideoParams("1", 1, 49082351, 1, 1, "test", 507)
        self.partial = self.progress(0.075, [{"s": 0, "e": 0}, {"s": 470, "e": 508}])

    def progress(self, rate, intervals, completed=False):
        return dict(rate=rate, intervals=intervals, completed=completed,
                    last_point=507, available=True)

    def test_reported_final_position_does_not_skip_missing_prefix(self):
        self.assertEqual(self.watcher._resume_position(self.params, self.partial), 0)

    def test_overlapping_intervals_resume_at_first_gap(self):
        progress = self.progress(0.3, [
            {"s": 50, "e": 507}, {"s": 10, "e": 30}, {"s": 0, "e": 20},
        ])
        self.assertEqual(self.watcher._resume_position(self.params, progress), 30)

    def test_missing_intervals_do_not_use_last_point_as_coverage(self):
        self.assertEqual(self.watcher._resume_position(self.params, self.progress(0.075, [])), 0)

    def test_contradictory_full_intervals_do_not_make_empty_retries(self):
        progress = self.progress(0.075, [{"s": 0, "e": 508}])
        self.assertEqual(self.watcher._resume_position(self.params, progress), 0)

    def test_invalid_intervals_are_ignored(self):
        progress = self.progress(0, [{"s": "nan", "e": 100}, {"s": 100, "e": 5}, None])
        self.assertEqual(self.watcher._resume_position(self.params, progress), 0)

    def test_page_id_stays_fixed_with_monotonic_sequence_and_real_waits(self):
        self.params.duration = 35
        sent, clock = [], [0.0]

        def pause(delay):
            clock[0] += delay

        def send(data, **kwargs):
            sent.extend((dict(item), clock[0]) for item in data)
            return True

        with patch.object(self.watcher, "_pause", side_effect=pause):
            with patch.object(self.watcher, "_send_heartbeat", side_effect=send):
                for _ in range(2):
                    self.assertTrue(self.watcher._play_pass(self.params, 1.5, 15, 0))
        split = next(i for i in range(1, len(sent)) if sent[i][0]["sq"] == 1)
        first, second = sent[:split], sent[split:]
        for batch in [first, second]:
            self.assertEqual(len({payload["pg"] for payload, _ in batch}), 1)
            self.assertEqual([p["sq"] for p, _ in batch], list(range(1, len(batch) + 1)))
            self.assertNotIn("waiting", [p["et"] for p, _ in batch])
            self.assertEqual(batch[-1][0]["cp"], 35)
        self.assertNotEqual(first[0][0]["pg"], second[0][0]["pg"])
        advanced, elapsed = next((p, t) for p, t in first if p["cp"] > 0)
        self.assertGreaterEqual(elapsed, advanced["cp"] / 1.5)

    def test_regular_progress_uses_heartbeat_and_batches_five_second_samples(self):
        self.params.duration = 35
        clock, batches = [0.0], []

        def pause(delay):
            clock[0] += delay

        def send(data, **kwargs):
            batches.append([dict(item) for item in data])
            return True

        with patch.object(self.watcher, "_pause", side_effect=pause):
            with patch("yuketang.video.time.monotonic", side_effect=lambda: clock[0]):
                with patch.object(self.watcher, "_send_heartbeat", side_effect=send):
                    self.assertTrue(self.watcher._play_pass(self.params, 1, 15, 0))
        records = [record for batch in batches for record in batch]
        self.assertEqual([r["cp"] for r in records if r["et"] == "heartbeat"], [5, 10, 15, 20, 25, 30, 35])
        self.assertEqual(sum(r["et"] == "playing" for r in records), 1)
        self.assertEqual([r["cp"] for r in batches[1]], [5, 10, 15])
        for record in records:
            self.assertEqual(record["fp"], 0)
            self.assertEqual(record["tp"], 0)
            self.assertEqual(record["i"], 5)

    def test_resuming_does_not_set_seek_fields_on_normal_playback(self):
        ordinary = self.watcher._build_heartbeat(self.params, "heartbeat", 50, 30, 1, page_id="test")
        self.assertEqual((ordinary["cp"], ordinary["fp"], ordinary["tp"]), (50, 0, 0))
        seek = self.watcher._build_heartbeat(self.params, "seeking", 50, 30, 1, page_id="test")
        self.assertEqual((seek["fp"], seek["tp"]), (30, 50))

    def test_failed_startup_does_not_advance_or_send_videoend(self):
        with patch.object(self.watcher, "_send_heartbeat", return_value=False) as send:
            self.assertFalse(self.watcher._play_pass(self.params, 1.5, 15, 0))
        self.assertEqual(send.call_args.args[0][0]["cp"], 0)
        self.assertEqual(send.call_count, 1)

    def test_nonzero_rate_is_failure_when_no_effective_progress(self):
        with patch.object(self.watcher, "_pause"):
            with patch.object(self.watcher, "_play_pass", return_value=True) as play:
                with patch.object(self.watcher, "get_progress", return_value=self.partial):
                    self.assertFalse(self.watcher.watch(self.params, initial_progress=self.partial))
        play.assert_called_once()

    def test_server_update_delay_is_polled_without_replaying(self):
        complete = self.progress(1, [{"s": 0, "e": 508}], True)
        with patch.object(self.watcher, "_pause"):
            with patch.object(self.watcher, "_play_pass", return_value=True) as play:
                with patch.object(self.watcher, "get_progress", side_effect=[self.partial, self.partial, complete]):
                    self.assertTrue(self.watcher.watch(self.params, initial_progress=self.partial))
        play.assert_called_once()

    def test_retry_plays_from_gap_even_when_last_point_is_at_end(self):
        initial = self.progress(0, [])
        partial = self.progress(0.5, [{"s": 0, "e": 20}, {"s": 300, "e": 508}])
        complete = self.progress(1, [{"s": 0, "e": 508}], True)
        with patch.object(self.watcher, "_pause"):
            with patch.object(self.watcher, "_play_pass", return_value=True) as play:
                with patch.object(self.watcher, "get_progress", side_effect=[partial] * 3 + [complete]):
                    self.assertTrue(self.watcher.watch(self.params, initial_progress=initial))
        self.assertEqual([call.args[3] for call in play.call_args_list], [0, 20])

    def test_query_failure_never_counts_as_complete(self):
        missing = dict(rate=0, completed=False, available=False)
        with patch.object(self.watcher, "_pause"):
            with patch.object(self.watcher, "_play_pass", return_value=True):
                with patch.object(self.watcher, "get_progress", return_value=missing):
                    self.assertFalse(self.watcher.watch(self.params, initial_progress=self.partial))

    def test_string_zero_completed_flag_is_false(self):
        response = {"data": {"heartbeat": {"rate": 0.075, "last_point": 507, "completed": "0", "result": [{"s": 470, "e": 508}]}}}
        with patch.object(self.session, "get_json", return_value=response):
            progress = self.watcher.get_progress(self.params)
        self.assertFalse(progress["completed"])
        self.assertTrue(progress["available"])
        self.assertFalse(self.watcher._is_complete(progress))

    def test_ninety_percent_does_not_skip_incomplete_video(self):
        course, leaf = Course("test", 1, "test", 1, 1), Leaf(1, "test", 0)
        progress = self.progress(0.95, [{"s": 0, "e": 481}])
        stats = dict(total=1, success=0, skipped=0, failed=0)
        with patch.object(self.watcher, "build_params", return_value=self.params):
            with patch.object(self.watcher, "get_progress", return_value=progress):
                with patch.object(self.watcher, "watch", return_value=False) as play:
                    self.assertEqual(self.watcher._watch_one(course, leaf, None, None, True, stats), "failed")
        self.assertEqual(stats["success"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(play.call_args.kwargs["start_position"], 481)

    def test_cli_exits_with_failure_when_video_remains_incomplete(self):
        config = Config(auto_richtext=False)
        args = argparse.Namespace(course="test", speed=None, interval=None, no_skip=False, workers=1)
        with patch("yuketang.cli._ensure_login"):
            with patch("yuketang.cli.CourseAPI"):
                with patch("yuketang.cli._select_courses", return_value=[Course("test", 1, "test", 1, 1)]):
                    with patch("yuketang.cli.VideoWatcher") as watcher:
                        watcher.return_value.watch_course.return_value = dict(total=1, success=0, skipped=0, failed=1)
                        self.assertEqual(cmd_video(config, args), 1)
                        self.assertEqual(cmd_all(config, args), 1)


if __name__ == "__main__":
    unittest.main()
