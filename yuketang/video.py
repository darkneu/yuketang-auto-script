# -*- coding: utf-8 -*-
"""视频自动观看模块。

核心原理：雨课堂通过 ``/video-log/heartbeat/`` 接口接收播放器心跳事件
（``loadstart`` / ``play`` / ``playing`` / ``waiting`` / ``videoend`` 等），
服务端据此累计观看时长与完成率。本模块通过构造合法的心跳序列，
在**不真正播放视频**的情况下完成学习进度上报。

关键点：
- 心跳数据需包含 ``u``(user_id) / ``c``(course_id) / ``v``(video_id) /
  ``skuid`` / ``classroomid`` / ``cc`` / ``d``(时长) 等字段。
- 播放位置 ``cp`` 需按倍速递增，且不能超过视频总时长。
- 需先发送 ``loadstart`` → ``loadeddata`` → ``play`` → ``playing``，
  结束时发送 ``videoend`` → ``pause``。
"""

from __future__ import annotations

import random
import string
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Config
from .course import Course, CourseAPI, Leaf
from .logger import get_logger, ok, warn
from .session import YuketangSession

logger = get_logger("video")


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
        self.course_api = CourseAPI(session, config.university_id)

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

        duration = self._resolve_duration(media, cc_id)

        params = VideoParams(
            user_id=user_id,
            course_id=course_id,
            video_id=leaf.leaf_id,
            sku_id=sku_id,
            classroom_id=course.classroom_id,
            cc_id=cc_id,
            duration=duration,
            university_id=self.config.university_id,
        )

        # 关键：以服务端进度接口返回的 video_length 为准。
        # media.duration 常为 0，播放地址解析出的时长也可能与实际不符，
        # 而服务端按 video_length 计算完成率，若客户端时长偏小会导致
        # 「播放到终点但完成率不足」。
        server_length = self._server_video_length(params)
        if server_length > 0 and abs(server_length - params.duration) > 1:
            logger.info(
                "校正视频时长：本地 %.0fs → 服务端 %.0fs",
                params.duration, server_length,
            )
            params.duration = server_length

        if not params.is_valid():
            logger.debug("视频参数不完整：%s", params)
            return None
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
            ``completed``（是否完成）的字典。
        """
        url = (
            f"{self.session.base_url}/video-log/detail/"
            f"?cid={params.course_id}&user_id={params.user_id}"
            f"&classroom_id={params.classroom_id}&video_id={params.video_id}"
            f"&video_type=video&term=latest&uv_id={params.university_id}"
        )
        headers = {"classroom-id": str(params.classroom_id), "Xt-Agent": "web"}
        data = self.session.get_json(url, headers=headers)
        result = {"rate": 0.0, "last_point": 0.0, "completed": False}
        if not data:
            return result

        heartbeat = (data.get("data") or {}).get("heartbeat") or {}
        if not heartbeat:
            return result

        rate = heartbeat.get("rate")
        result["rate"] = float(rate) if rate is not None else 0.0
        result["last_point"] = float(heartbeat.get("last_point") or 0.0)
        result["completed"] = bool(heartbeat.get("completed"))
        return result

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
    ) -> Dict[str, Any]:
        """构造单条心跳数据。"""
        return {
            "i": 5,
            "et": event,
            "p": "web",
            "n": "ali-cdn.xuetangx.com",
            "lob": "cloud4",
            "cp": round(position, 2),
            "fp": round(first_position, 2),
            "tp": round(position, 2),
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
            "pg": f"{params.video_id}_{self._random_suffix()}",
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
        url = f"{self.session.base_url}/video-log/heartbeat/"
        headers = {}
        if classroom_id is not None:
            headers["classroom-id"] = str(classroom_id)
        resp = self.session.post_json(url, payload={"heart_data": data}, headers=headers)
        if resp is None:
            return False
        # 雨课堂成功时返回空对象 {} 或 code=0 / success=true
        if resp == {} or resp.get("code") == 0 or resp.get("success"):
            return True
        logger.debug("心跳响应异常：%s", resp)
        return False

    # ==================================================================
    #  观看模拟
    # ==================================================================
    def watch(
        self,
        params: VideoParams,
        speed: Optional[float] = None,
        interval: Optional[int] = None,
        start_position: float = 0.0,
    ) -> bool:
        """模拟完整观看一个视频。

        Args:
            params: 视频参数。
            speed: 播放倍速，默认取配置值。
            interval: 心跳间隔（秒），默认取配置值。
            start_position: 起始播放位置（秒），用于断点续看。
        """
        speed = speed or self.config.video_speed
        interval = interval or self.config.heartbeat_interval
        duration = params.duration

        if start_position >= duration:
            start_position = 0.0

        logger.info(
            "开始观看视频 id=%s 时长=%.0fs 倍速=%.1fx 起点=%.0fs",
            params.video_id, duration, speed, start_position,
        )

        position = start_position
        first_position = start_position
        seq = 1

        # 每次心跳前进的秒数。
        # 服务端会校验「两次心跳的真实时间间隔」与「cp 增量」是否匹配：
        # 真实经过 interval 秒，最多只认可 interval * speed 秒的进度。
        # 因此 step 必须 <= interval * speed，并留出安全余量（网络延迟、
        # 心跳发送耗时都会让真实间隔略大于 interval，若 step 取满会超速被丢弃）。
        # 这里取 0.9 的安全系数，避免因超速导致完成率不足。
        step = max(interval * speed * 0.9, 1.0)

        logger.debug(
            "心跳步长 step=%.2fs（interval=%ss × speed=%.1fx × 0.9）",
            step, interval, speed,
        )

        # ---- 起始事件序列 ----
        for event in ("loadstart", "loadeddata", "play", "playing"):
            self._send_heartbeat(
                [self._build_heartbeat(params, event, position, first_position, speed, seq)],
                classroom_id=params.classroom_id,
            )
            seq += 1
            time.sleep(0.2)

        # ---- 播放过程心跳 ----
        while position < duration:
            position = min(position + step, duration)

            # 大部分时间为 playing，偶尔插入 waiting 更贴近真实
            event = random.choices(
                ["playing", "waiting"], weights=[0.9, 0.1], k=1
            )[0]

            success = self._send_heartbeat(
                [self._build_heartbeat(params, event, position, first_position, speed, seq)],
                classroom_id=params.classroom_id,
            )
            seq += 1

            if not success:
                warn(f"心跳发送失败（位置 {position:.0f}s），继续尝试…")

            if seq % 6 == 0:
                logger.info(
                    "  进度 %.1f%% (%.0f/%.0fs)",
                    position / duration * 100, position, duration,
                )

            # 节流：避免触发服务端限流
            time.sleep(interval)

        # ---- 结束事件序列 ----
        for event in ("videoend", "pause"):
            self._send_heartbeat(
                [self._build_heartbeat(params, event, duration, first_position, speed, seq)],
                classroom_id=params.classroom_id,
            )
            seq += 1
            time.sleep(0.2)

        # ---- 校验结果，不足则补发心跳 ----
        time.sleep(1)
        progress = self.get_progress(params)
        rate = progress.get("rate", 0.0)

        # 若完成率不足，说明部分心跳被服务端按超速丢弃，
        # 此时以「服务端认可的进度」为基准，按安全步长补发心跳直至达标。
        retry_round = 0
        while rate < 0.9 and retry_round < 5:
            retry_round += 1
            server_point = progress.get("last_point", 0.0)
            # 从服务端认可的位置继续，避免重复上报已认可区间
            position = min(server_point, duration)
            logger.info(
                "完成率 %.1f%% 不足，第 %d 轮补发心跳（从 %.0fs 继续）",
                rate * 100, retry_round, position,
            )
            while position < duration:
                position = min(position + step, duration)
                self._send_heartbeat(
                    [self._build_heartbeat(params, "playing", position, first_position, speed, seq)],
                    classroom_id=params.classroom_id,
                )
                seq += 1
                time.sleep(interval)
            # 补发结束事件
            for event in ("videoend", "pause"):
                self._send_heartbeat(
                    [self._build_heartbeat(params, event, duration, first_position, speed, seq)],
                    classroom_id=params.classroom_id,
                )
                seq += 1
                time.sleep(0.2)
            time.sleep(1)
            progress = self.get_progress(params)
            rate = progress.get("rate", 0.0)

        if rate >= 0.9:
            ok(f"视频 {params.video_id} 观看完成，完成率 {rate:.1%}")
            return True
        warn(f"视频 {params.video_id} 完成率仅 {rate:.1%}，可能需重试")
        return rate > 0

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
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        self._watch_one,
                        course, leaf, speed, interval, skip_completed, None,
                    ): leaf
                    for leaf in leaves
                }
                for future in as_completed(futures):
                    leaf = futures[future]
                    try:
                        status = future.result()
                    except Exception as exc:  # pragma: no cover
                        logger.error("视频 %s 处理异常：%s", leaf.name, exc)
                        status = "failed"
                    stats[status] = stats.get(status, 0) + 1

        logger.info(
            "课程《%s》完成：成功 %d / 跳过 %d / 失败 %d（共 %d）",
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
        params = self.build_params(course, leaf)
        if not params:
            if stats is not None:
                stats["failed"] += 1
            return "failed"

        if skip_completed:
            progress = self.get_progress(params)
            if progress.get("rate", 0) >= 0.9:
                logger.info("⏭️  已完成（%.1f%%），跳过：%s", progress["rate"] * 100, leaf.name)
                if stats is not None:
                    stats["skipped"] += 1
                return "skipped"

        # 断点续看：从服务端认可的位置继续，避免重复上报已认可区间。
        # 注意不要回退（如 last_point - 10），否则会与已认可区间重叠，
        # 服务端可能因位置回退而拒绝后续心跳。
        start = 0.0
        if skip_completed:
            progress = self.get_progress(params)
            last_point = progress.get("last_point", 0.0)
            if last_point > 0:
                start = min(last_point, params.duration)

        try:
            success = self.watch(params, speed=speed, interval=interval, start_position=start)
        except Exception as exc:
            logger.error("视频 %s 观看异常：%s", leaf.name, exc)
            success = False

        status = "success" if success else "failed"
        if stats is not None:
            stats[status] += 1
        return status
