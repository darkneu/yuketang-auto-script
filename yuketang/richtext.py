# -*- coding: utf-8 -*-
"""图文（课件）与讨论区自动完成模块。

- **图文**：调用 ``user_article_finish`` 接口标记已读。
- **讨论**：调用讨论接口发帖，内容由 DeepSeek 生成。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from .ai import DeepSeekClient
from .config import Config
from .course import LEAF_DISCUSSION, LEAF_RICHTEXT, Course, CourseAPI, Leaf
from .logger import get_logger, ok, warn
from .session import YuketangSession

logger = get_logger("richtext")


class RichtextWorker:
    """图文打卡器。"""

    def __init__(self, session: YuketangSession, config: Config) -> None:
        self.session = session
        self.config = config
        self.course_api = CourseAPI(session, config.university_id)

    # ------------------------------------------------------------------
    def finish_one(self, course: Course, leaf: Leaf, stay_seconds: Optional[int] = None) -> bool:
        """完成单个图文打卡。"""
        stay_seconds = (
            self.config.richtext_stay_seconds if stay_seconds is None else stay_seconds
        )
        base = self.session.base_url
        status_url = f"{base}/mooc-api/v1/lms/learn/user_article_finish_status/{leaf.leaf_id}/"
        finish_url = f"{base}/mooc-api/v1/lms/learn/user_article_finish/{leaf.leaf_id}/"

        headers = {
            "classroom-id": str(course.classroom_id),
            "Xtbz": "ykt",
            "X-Client": "web",
        }

        # 已读则跳过
        status = self.session.get_json(status_url, headers=headers)
        if status and status.get("success"):
            if (status.get("data") or {}).get("finish") == 1:
                logger.info("⏭️  图文已完成，跳过：%s", leaf.name)
                return True

        if stay_seconds > 0:
            logger.debug("模拟阅读停留 %d 秒…", stay_seconds)
            time.sleep(stay_seconds)

        result = self.session.get_json(finish_url, headers=headers)
        if result and result.get("success"):
            ok(f"图文打卡完成：{leaf.name}")
            return True
        warn(f"图文打卡失败：{leaf.name} -> {result}")
        return False

    # ------------------------------------------------------------------
    def finish_course(self, course: Course) -> Dict[str, int]:
        """完成课程中所有图文。"""
        leaves = self.course_api.list_leaves(course, leaf_type=LEAF_RICHTEXT)
        if not leaves:
            logger.info("课程《%s》中没有图文内容", course.course_name)
            return {"total": 0, "success": 0, "failed": 0}

        if self.config.test_mode:
            leaves = leaves[: self.config.test_task_count]

        logger.info("课程《%s》共 %d 篇图文", course.course_name, len(leaves))
        stats = {"total": len(leaves), "success": 0, "failed": 0}
        for index, leaf in enumerate(leaves, 1):
            logger.info("── [%d/%d] %s", index, len(leaves), leaf.name)
            if self.finish_one(course, leaf):
                stats["success"] += 1
            else:
                stats["failed"] += 1
            time.sleep(0.5)

        logger.info(
            "图文完成：成功 %d / 失败 %d（共 %d）",
            stats["success"], stats["failed"], stats["total"],
        )
        return stats


class DiscussionWorker:
    """讨论区自动发帖器。"""

    def __init__(
        self,
        session: YuketangSession,
        config: Config,
        ai: Optional[DeepSeekClient] = None,
    ) -> None:
        self.session = session
        self.config = config
        self.ai = ai
        self.course_api = CourseAPI(session, config.university_id)

    # ------------------------------------------------------------------
    def _get_topic(self, course: Course, leaf: Leaf) -> Optional[Dict[str, Any]]:
        """获取讨论话题信息。"""
        info = self.course_api.get_leaf_info(course, leaf.leaf_id)
        if not info:
            return None

        content_info = info.get("content_info", {}) or {}
        context = content_info.get("context", "")
        text = ""
        if context:
            try:
                text = BeautifulSoup(context, "html.parser").get_text(" ", strip=True)
            except Exception:  # pragma: no cover
                text = str(context)

        return {
            "text": text,
            "sku_id": info.get("sku_id"),
            "leaf_id": info.get("id", leaf.leaf_id),
            "finish": info.get("finish", False),
        }

    # ------------------------------------------------------------------
    def _get_discussion_target(self, course: Course, leaf: Leaf, sku_id: Any) -> Optional[Dict[str, Any]]:
        """获取讨论帖的 topic_id 与 to_user。"""
        timestamp = int(time.time() * 1000)
        url = (
            f"{self.session.base_url}/v/discussion/v2/unit/discussion/"
            f"?_date={timestamp}&term=latest"
            f"&classroom_id={course.classroom_id}"
            f"&sku_id={sku_id}&leaf_id={leaf.leaf_id}"
            f"&topic_type=4&channel=xt"
        )
        data = self.session.get_json(url)
        if not data:
            return None
        payload = data.get("data") or {}
        if not payload.get("id"):
            return None
        return {"topic_id": payload["id"], "to_user": payload.get("user_id")}

    # ------------------------------------------------------------------
    def post_one(self, course: Course, leaf: Leaf) -> bool:
        """完成单个讨论发帖。"""
        topic = self._get_topic(course, leaf)
        if not topic:
            warn(f"无法获取讨论内容：{leaf.name}")
            return False

        if topic.get("finish"):
            logger.info("⏭️  讨论已完成，跳过：%s", leaf.name)
            return True

        if not self.ai or not self.ai.available:
            warn("未配置 DeepSeek，跳过讨论发帖")
            return False

        target = self._get_discussion_target(course, leaf, topic.get("sku_id"))
        if not target:
            warn(f"无法获取讨论目标：{leaf.name}")
            return False

        answer = self.ai.answer_discussion(topic["text"])
        logger.info("AI 生成讨论内容（%d 字）", len(answer))

        payload = {
            "to_user": target["to_user"],
            "topic_id": target["topic_id"],
            "content": {
                "text": answer,
                "upload_images": [],
                "accessory_list": [],
            },
            "anchor": 0,
        }
        url = (
            f"{self.session.base_url}/v/discussion/v2/comment/"
            f"?term=latest&uv_id={self.config.university_id}"
        )
        result = self.session.post_json(url, payload=payload)
        if result and result.get("success"):
            ok(f"讨论发帖成功：{leaf.name}")
            return True
        warn(f"讨论发帖失败：{leaf.name} -> {result}")
        return False

    # ------------------------------------------------------------------
    def post_course(self, course: Course) -> Dict[str, int]:
        """完成课程中所有讨论。"""
        leaves = self.course_api.list_leaves(course, leaf_type=LEAF_DISCUSSION)
        if not leaves:
            logger.info("课程《%s》中没有讨论", course.course_name)
            return {"total": 0, "success": 0, "failed": 0}

        if self.config.test_mode:
            leaves = leaves[: self.config.test_task_count]

        logger.info("课程《%s》共 %d 个讨论", course.course_name, len(leaves))
        stats = {"total": len(leaves), "success": 0, "failed": 0}
        for index, leaf in enumerate(leaves, 1):
            logger.info("── [%d/%d] %s", index, len(leaves), leaf.name)
            if self.post_one(course, leaf):
                stats["success"] += 1
            else:
                stats["failed"] += 1
            time.sleep(1)

        logger.info(
            "讨论完成：成功 %d / 失败 %d（共 %d）",
            stats["success"], stats["failed"], stats["total"],
        )
        return stats
