# -*- coding: utf-8 -*-
"""视频自动观看模块。

核心原理：雨课堂通过 ``/video-log/heartbeat/`` 接口接收播放器心跳事件
（``loadstart`` / ``play`` / ``playing`` / ``heartbeat`` / ``videoend`` 等），
服务端据此累计观看时长与完成率。本模块通过构造合法的心跳序列，
在**不真正播放视频**的情况下完成学习进度上报。

关键点：
- 心跳数据需包含 ``u``(user_id) / ``c``(course_id) / ``v``(video_id) /
  ``skuid`` / ``classroomid`` / ``cc`` / ``d``(时长) 等字段。
- 播放位置 ``cp`` 需按倍速递增，且不能超过视频总时长。
- 需先发送 ``loadstart`` → ``loadeddata`` → ``play`` → ``playing``，
  播放中每 5 秒采样 ``heartbeat``，结束时发送 ``videoend`` → ``pause``。
- 普通事件的 ``fp`` / ``tp`` 为 0，仅 ``seeking`` 事件填写拖动起止位置。
"""

from __future__ import annotations

import math
import random
import string
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Config
from .course import Course, CourseAPI, Leaf
from .logger import get_logger, ok, warn
from .session import YuketangSession

logger = get_logger("video")


class _VideoCancelled(Exception):
    """视频任务收到停止请求。"""


@dataclass
class VideoParams:
    """单个视频播放所需的全部参数。"""

    user_id: str
    course_id: int
    video_id: int
    sku_id: int
    classroom_id: int
    cc_id: str
    duration: float
    university_id: str = ""
    initial_progress: Optional[Dict[str, Any]] = None

    def is_valid(self) -> bool:
        return all(
            [
                self.user_id,
                self.course_id,
                self.video_id,
                self.sku_id,
                self.classroom_id,
                self.cc_id,
                self.duration > 0,
            ]
        )


class VideoWatcher:
    """视频心跳刷课器。"""

    def __init__(self, session: YuketangSession, config: Config) -> None:
        self.session = session
        self.config = config
        self.course_api = CourseAPI(session, config.effective_university_id)
        self._stop_event = session.stop_event

    def _pause(self, seconds: float) -> None:
        """分段等待，兼容主线程 Ctrl+C 与工作线程协作取消。"""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._stop_event.wait(min(0.2, remaining)):
                raise _VideoCancelled()

    # ==================================================================
    #  参数获取
    # ==================================================================
    def build_params(self, course: Course, leaf: Leaf) -> Optional[VideoParams]:
        """根据课程与叶子节点构造视频播放参数。"""
        info = self.course_api.get_leaf_info(course, leaf.leaf_id)
        if not info:
            warn(f"无法获取视频信息：{leaf.name}")
            return None

        content_info = info.get("content_info", {}) or {}
        media = content_info.get("media", {}) or {}

        user_id = str(info.get("user_id") or self.config.user_id or "")
        course_id = int(info.get("course_id") or course.course_id or 0)
        sku_id = int(info.get("sku_id") or leaf.sku_id or course.sku_id or 0)
        cc_id = str(
            media.get("ccid")
            or media.get("cc_id")
            or media.get("cc")
            or media.get("video_id")
            or ""
        )

        params = VideoParams(
            user_id=user_id,
            course_id=course_id,
            video_id=leaf.leaf_id,
            sku_id=sku_id,
            classroom_id=course.classroom_id,
            cc_id=cc_id,
            duration=0,
            university_id=self.config.effective_university_id,
        )

        # 关键：以服务端进度接口返回的 video_length 为准。
        # media.duration 常为 0，播放地址解析出的时长也可能与实际不符，
        # 而服务端按 video_length 计算完成率，若客户端时长偏小会导致
        # 「播放到终点但完成率不足」。
        server_length = self._server_video_length(params)
        params.duration = server_length if server_length > 0 else self._resolve_duration(media, cc_id)

        if not params.is_valid():
            logger.debug("视频参数不完整：%s", params)
            return None
        self.session.emit(leaf.leaf_id, duration=params.duration)
        return params

    # ------------------------------------------------------------------
    def _server_video_length(self, params: VideoParams) -> float:
        """从服务端进度接口读取权威的视频时长（``video_length``）。"""
        url = (
            f"{self.session.base_url}/video-log/detail/"
            f"?cid={params.course_id}&user_id={params.user_id}"
            f"&classroom_id={params.classroom_id}&video_id={params.video_id}"
            f"&video_type=video&term=latest&uv_id={params.university_id}"
        )
        headers = {"classroom-id": str(params.classroom_id), "Xt-Agent": "web"}
        data = self.session.get_json(url, headers=headers)
        params.initial_progress = self._parse_progress(params, data)
        if not data:
            return 0.0
        heartbeat = (data.get("data") or {}).get("heartbeat") or {}
        try:
            return float(heartbeat.get("video_length") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    def _resolve_duration(self, media: Dict[str, Any], cc_id: str) -> float:
        """解析视频时长（秒）。"""
        for key in ("duration", "video_duration", "length"):
            value = media.get(key)
            if value:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass

        # 回退：从播放地址解析
        if cc_id:
            duration = self._duration_from_play_url(cc_id)
            if duration > 0:
                return duration

        # 无法解析时返回 0，交由 build_params 用服务端 video_length 校正。
        # 切勿返回硬编码默认值（如 600），否则会因时长偏小导致完成率不足。
        logger.debug("无法从 media/播放地址解析视频时长，等待服务端校正")
        return 0.0

    # ------------------------------------------------------------------
    def _duration_from_play_url(self, cc_id: str) -> float:
        """通过播放地址的 moov 信息解析时长。"""
        url = (
            f"{self.session.base_url}/api/v3/video-play/get-video-play-url/"
            f"?video_id={cc_id}&provider=cc&file_type=1&is_single=0"
        )
        data = self.session.get_json(url)
        if not data:
            return 0.0
        try:
            sources = data["data"]["playurl"]["sources"]
            play_url = next(iter(sources.values()))[0]
        except (KeyError, IndexError, TypeError, StopIteration):
            return 0.0
        return self._parse_mp4_duration(play_url)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_mp4_duration(url: str) -> float:
        """通过 HTTP Range 读取 MP4 的 mvhd box 解析时长。"""
        import struct

        import requests

        try:
            # 读取文件头，定位 moov/mvhd
            resp = requests.get(url, headers={"Range": "bytes=0-1023"}, timeout=10)
            head = resp.content
            idx = head.find(b"mvhd")
            if idx == -1:
                # moov 可能在文件尾部，尝试读取尾部
                resp = requests.get(
                    url, headers={"Range": "bytes=-65536"}, timeout=10
                )
                head = resp.content
                idx = head.find(b"mvhd")
            if idx == -1:
                return 0.0

            # mvhd box: version(1) + flags(3) + ...
            version = head[idx + 4]
            if version == 1:
                timescale = struct.unpack(">I", head[idx + 24 : idx + 28])[0]
                duration = struct.unpack(">Q", head[idx + 28 : idx + 36])[0]
            else:
                timescale = struct.unpack(">I", head[idx + 16 : idx + 20])[0]
                duration = struct.unpack(">I", head[idx + 20 : idx + 24])[0]

            if timescale:
                return float(duration) / float(timescale)
        except Exception as exc:  # pragma: no cover
            logger.debug("解析视频时长失败：%s", exc)
        return 0.0

    # ==================================================================
    #  进度查询
    # ==================================================================
    def get_progress(self, params: VideoParams) -> Dict[str, Any]:
        """查询视频观看进度。

        使用 ``/video-log/detail/`` 接口，返回 ``heartbeat`` 字段。

        Returns:
            含 ``rate``（完成率 0~1）、``last_point``（最后位置秒）、
            ``completed``、``intervals``（已观看区间）与 ``available`` 的字典。
        """
        url = (
            f"{self.session.base_url}/video-log/detail/"
            f"?cid={params.course_id}&user_id={params.user_id}"
            f"&classroom_id={params.classroom_id}&video_id={params.video_id}"
            f"&video_type=video&term=latest&uv_id={params.university_id}"
        )
        headers = {"classroom-id": str(params.classroom_id), "Xt-Agent": "web"}
        data = self.session.get_json(url, headers=headers)
        return self._parse_progress(params, data)

    def _parse_progress(self, params: VideoParams, data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        result = {
            "rate": 0.0, "last_point": 0.0, "completed": False,
            "intervals": [], "available": False,
        }
        if not data or data.get("success") is False or not isinstance(data.get("data"), dict):
            return result

        heartbeat = (data.get("data") or {}).get("heartbeat") or {}
        if not isinstance(heartbeat, dict):
            return result
        try:
            rate = float(heartbeat.get("rate") or 0.0)
            last_point = float(heartbeat.get("last_point") or 0.0)
        except (TypeError, ValueError):
            return result
        if not math.isfinite(rate) or not math.isfinite(last_point):
            return result
        intervals = heartbeat.get("result")
        result.update(
            rate=max(0.0, min(rate, 1.0)),
            last_point=max(0.0, last_point),
            completed=str(heartbeat.get("completed", "")).lower() in {"1", "true"},
            intervals=intervals if isinstance(intervals, list) else [],
            available=True,
        )
        self.session.emit(params.video_id,
                          server_rate=result["rate"] if "rate" in heartbeat else None,
                          server_completed=(self._is_complete(result) if "rate" in heartbeat or "completed" in heartbeat else None))
        return result

    @staticmethod
    def _is_complete(progress: Dict[str, Any]) -> bool:
        """只把服务端完成标记或完整的观看比例作为成功。"""
        return bool(progress.get("completed")) or progress.get("rate", 0.0) >= 1.0

    @staticmethod
    def _resume_position(params: VideoParams, progress: Dict[str, Any]) -> float:
        """找出从开头连续覆盖区间的终点，而非最后一次上报的播放位置。"""
        intervals = []
        for item in progress.get("intervals", []):
            if not isinstance(item, dict):
                continue
            try:
                start, end = float(item["s"]), float(item["e"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                continue
            start = max(0.0, min(start, params.duration))
            end = max(0.0, min(end, params.duration))
            intervals.append((start, end))
        covered = 0.0
        for start, end in sorted(intervals):
            if start > covered:
                break
            covered = max(covered, end)
        # 区间与完成标记矛盾时重新核实整段，不能空转重复发送结束事件。
        return covered if covered < params.duration else 0.0

    # ==================================================================
    #  心跳构造与发送
    # ==================================================================
    def _build_heartbeat(
        self,
        params: VideoParams,
        event: str,
        position: float,
        first_position: float,
        speed: float,
        seq: int = 1,
        *,
        page_id: str,
    ) -> Dict[str, Any]:
        """构造单条心跳数据。"""
        self.session.emit(params.video_id, position=position, duration=params.duration)
        return {
            "i": 5,
            "et": event,
            "p": "web",
            "n": "ali-cdn.xuetangx.com",
            "lob": "cloud4",
            "cp": round(position, 2),
            # fp/tp 是 seeking 的起止位置，普通播放事件必须为 0。
            "fp": round(first_position, 2) if event == "seeking" else 0,
            "tp": round(position, 2) if event == "seeking" else 0,
            "sp": speed,
            "ts": str(int(time.time() * 1000)),
            "u": int(params.user_id) if str(params.user_id).isdigit() else params.user_id,
            "uip": "",
            "c": params.course_id,
            "v": params.video_id,
            "skuid": params.sku_id,
            "classroomid": str(params.classroom_id),
            "cc": params.cc_id,
            "d": params.duration,
            "pg": page_id,
            "sq": seq,
            "t": "video",
        }

    @staticmethod
    def _random_suffix(length: int = 4) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.sample(alphabet, length))

    # ------------------------------------------------------------------
    def _send_heartbeat(self, data: List[Dict[str, Any]], classroom_id: Any = None) -> bool:
        """发送一批心跳数据。"""
        if self._stop_event.is_set():
            raise _VideoCancelled()
        url = f"{self.session.base_url}/video-log/heartbeat/"
        headers = {}
        if classroom_id is not None:
            headers["classroom-id"] = str(classroom_id)
        resp = self.session.post_json(url, payload={"heart_data": data}, headers=headers)
        if resp is None:
            return False
        # 雨课堂成功时返回空对象 {} 或 code=0 / success=true
        if resp.get("success") is False:
            logger.warning("心跳被服务端拒绝：%s", resp.get("msg") or resp.get("message") or resp.get("code"))
            return False
        if resp == {} or resp.get("code") == 0 or resp.get("success") is True:
            return True
        logger.debug("心跳响应异常：%s", resp)
        return False

    # ==================================================================
    #  观看模拟
    # ==================================================================
    def _play_pass(
        self, params: VideoParams, speed: float, interval: float, start_position: float,
    ) -> bool:
        """每 5 秒采样 heartbeat，按配置间隔批量提交；playing 仅用于开始播放。"""
        position = start_position
        first_position = start_position
        page_id = f"{params.video_id}_{self._random_suffix(12)}"
        seq = 0
        sample_interval = 5.0
        post_interval = max(sample_interval, float(interval))
        pending: List[Dict[str, Any]] = []
        last_post = time.monotonic()

        def record(event: str) -> None:
            nonlocal seq
            seq += 1
            payload = self._build_heartbeat(
                params, event, position, first_position, speed, seq, page_id=page_id,
            )
            pending.append(payload)

        def flush() -> bool:
            nonlocal last_post
            if not self._send_heartbeat(list(pending), classroom_id=params.classroom_id):
                return False
            pending.clear()
            last_post = time.monotonic()
            return True

        for event in ("loadstart", "loadeddata", "play", "playing"):
            record(event)
            self._pause(0.2)
        if not flush():
            warn(f"视频 {params.video_id} 启动心跳失败，停止本轮播放")
            return False

        step = sample_interval * speed
        while position < params.duration:
            advance = min(step, params.duration - position)
            self._pause(advance / speed)
            position = min(position + advance, params.duration)
            record("heartbeat")
            if time.monotonic() - last_post >= post_interval or position >= params.duration:
                if not flush():
                    warn(f"视频 {params.video_id} 心跳失败（位置 {position:.0f}s），停止本轮播放")
                    return False
            if seq % 6 == 0 or position >= params.duration:
                logger.info(
                    "视频 %s 上报播放位置 %.1f%% (%.0f/%.0fs)，完成状态待服务端校验",
                    params.video_id, position / params.duration * 100,
                    position, params.duration,
                )

        for event in ("videoend", "pause"):
            record(event)
            self._pause(0.2)
        return flush()

    def watch(
        self,
        params: VideoParams,
        speed: Optional[float] = None,
        interval: Optional[int] = None,
        start_position: float = 0.0,
        initial_progress: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """播放并核实服务端覆盖率；只有确认完成才返回 True。"""
        speed = self.config.video_speed if speed is None else speed
        interval = self.config.heartbeat_interval if interval is None else interval
        if not math.isfinite(speed) or not math.isfinite(interval) or speed <= 0 or interval <= 0:
            raise ValueError("视频倍速和心跳间隔必须大于 0")
        progress = initial_progress if initial_progress is not None else self.get_progress(params)
        start = max(0.0, min(start_position, params.duration))
        if start >= params.duration:
            start = self._resume_position(params, progress)

        for attempt in range(1, 4):
            logger.info(
                "开始观看视频 id=%s 时长=%.0fs 倍速=%.1fx 起点=%.0fs（第 %d/3 轮）",
                params.video_id, params.duration, speed, start, attempt,
            )
            previous_rate = progress.get("rate", 0.0)
            sent = self._play_pass(params, speed, interval, start)

            # 进度写入可能稍有延迟，只查询确认，不空转补发 videoend。
            for check in range(3):
                self._pause(1 if check == 0 else 2)
                progress = self.get_progress(params)
                if self._is_complete(progress):
                    ok(f"视频 {params.video_id} 服务端确认完成，完成率 {progress['rate']:.1%}")
                    return True

            if not progress.get("available"):
                warn(f"视频 {params.video_id} 无法读取服务端进度，本次不计为完成")
                return False
            rate = progress["rate"]
            next_start = self._resume_position(params, progress)
            logger.warning(
                "视频 %s 服务端完成率 %.1f%%，最后上报位置 %.0fs，首个未覆盖位置 %.0fs",
                params.video_id, rate * 100, progress["last_point"], next_start,
            )
            if not sent:
                warn(f"视频 {params.video_id} 心跳提交失败，请检查连接后重试")
                return False
            if rate <= previous_rate + 0.000001:
                warn(f"视频 {params.video_id} 本轮未增加有效观看进度，停止自动重放，本次计为失败")
                return False
            start = next_start

        warn(f"视频 {params.video_id} 三轮后仍未完成（{progress['rate']:.1%}），本次计为失败")
        return False

    # ==================================================================
    #  批量刷课
    # ==================================================================
    def watch_course(
        self,
        course: Course,
        speed: Optional[float] = None,
        interval: Optional[int] = None,
        skip_completed: Optional[bool] = None,
        max_workers: Optional[int] = None,
    ) -> Dict[str, int]:
        """刷完一门课程中的所有视频。

        Returns:
            统计字典：``total`` / ``success`` / ``skipped`` / ``failed``。
        """
        skip_completed = (
            self.config.skip_completed if skip_completed is None else skip_completed
        )
        max_workers = max_workers or self.config.max_concurrent_videos

        leaves = self.course_api.list_leaves(course, leaf_type=0)
        if not leaves:
            warn(f"课程《{course.course_name}》中没有找到视频")
            return {"total": 0, "success": 0, "skipped": 0, "failed": 0}

        if self.config.test_mode:
            leaves = leaves[: self.config.test_task_count]
            logger.info("测试模式：仅处理前 %d 个视频", len(leaves))

        logger.info("课程《%s》共 %d 个视频", course.course_name, len(leaves))

        stats = {"total": len(leaves), "success": 0, "skipped": 0, "failed": 0}

        if max_workers <= 1:
            for index, leaf in enumerate(leaves, 1):
                logger.info("── [%d/%d] %s", index, len(leaves), leaf.name)
                self._watch_one(course, leaf, speed, interval, skip_completed, stats)
        else:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            futures = {}
            try:
                futures = {
                    executor.submit(
                        self._watch_one,
                        course, leaf, speed, interval, skip_completed, None,
                    ): leaf
                    for leaf in leaves
                }
                pending = set(futures)
                while pending:
                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        leaf = futures[future]
                        try:
                            status = future.result()
                        except Exception as exc:  # pragma: no cover
                            logger.error("视频 %s 处理异常：%s", leaf.name, exc)
                            status = "failed"
                        stats[status] = stats.get(status, 0) + 1
            except KeyboardInterrupt:
                self._stop_event.set()
                for future in futures:
                    future.cancel()
                warn("正在停止视频任务；已发出的网络请求需等待返回或超时…")
                raise
            finally:
                executor.shutdown(wait=True)

        logger.info(
            "课程《%s》处理结束：成功 %d / 跳过 %d / 失败 %d（共 %d）",
            course.course_name,
            stats["success"], stats["skipped"], stats["failed"], stats["total"],
        )
        return stats

    # ------------------------------------------------------------------
    def _watch_one(
        self,
        course: Course,
        leaf: Leaf,
        speed: Optional[float],
        interval: Optional[int],
        skip_completed: bool,
        stats: Optional[Dict[str, int]],
    ) -> str:
        """处理单个视频，返回状态字符串。"""
        if self._stop_event.is_set():
            return "skipped"
        params = self.build_params(course, leaf)
        if not params:
            if stats is not None:
                stats["failed"] += 1
            return "failed"

        progress = params.initial_progress if params.initial_progress is not None else self.get_progress(params)
        params.initial_progress = None
        if not progress.get("available"):
            warn(f"无法读取视频 {params.video_id} 的服务端进度，跳过本次播放并计为失败")
            if stats is not None:
                stats["failed"] += 1
            return "failed"
        if skip_completed:
            if self._is_complete(progress):
                logger.info("⏭️  已完成（%.1f%%），跳过：%s", progress["rate"] * 100, leaf.name)
                if stats is not None:
                    stats["skipped"] += 1
                return "skipped"

        # last_point 只是播放头位置，可能已在结尾，但中间存在大量未观看区间。
        start = self._resume_position(params, progress) if skip_completed else 0.0

        try:
            success = self.watch(
                params, speed=speed, interval=interval,
                start_position=start, initial_progress=progress,
            )
        except _VideoCancelled:
            return "skipped"
        except Exception as exc:
            logger.error("视频 %s 观看异常：%s", leaf.name, exc)
            success = False

        status = "success" if success else "failed"
        if stats is not None:
            stats[status] += 1
        return status
