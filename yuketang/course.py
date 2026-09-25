# -*- coding: utf-8 -*-
"""课程与章节 API。

封装雨课堂课程列表、章节结构、叶子节点（视频/图文/作业/讨论）的获取与解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .logger import get_logger
from .session import YuketangSession

logger = get_logger("course")

# 叶子节点类型（leaf_type）
LEAF_VIDEO = 0
LEAF_RICHTEXT = 3
LEAF_DISCUSSION = 4
LEAF_EXAM = 5
LEAF_HOMEWORK = 6

LEAF_TYPE_NAMES = {
    LEAF_VIDEO: "视频",
    LEAF_RICHTEXT: "图文",
    LEAF_DISCUSSION: "讨论",
    LEAF_EXAM: "考试",
    LEAF_HOMEWORK: "作业",
}


@dataclass
class Course:
    """课程信息。"""

    course_name: str
    classroom_id: int
    course_sign: str
    sku_id: int
    course_id: int
    university_id: str = ""

    def __str__(self) -> str:
        return f"{self.course_name} (classroom_id={self.classroom_id})"


@dataclass
class Leaf:
    """课程章节中的叶子节点（可学习单元）。"""

    leaf_id: int
    name: str
    leaf_type: int
    chapter_name: str = ""
    section_id: int = 0
    sku_id: int = 0
    leafinfo_id: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def type_name(self) -> str:
        return LEAF_TYPE_NAMES.get(self.leaf_type, f"未知({self.leaf_type})")

    def __str__(self) -> str:
        return f"[{self.type_name}] {self.name} (id={self.leaf_id})"


class CourseAPI:
    """课程相关接口封装。"""

    def __init__(self, session: YuketangSession, university_id: str = "") -> None:
        self.session = session
        self.university_id = str(university_id or session.university_id)

    # ------------------------------------------------------------------
    def list_courses(self) -> List[Course]:
        """获取当前用户所有进行中的课程。"""
        url = f"{self.session.base_url}/v2/api/web/courses/list?identity=2"
        data = self.session.get_json(url)
        if not data:
            logger.error("获取课程列表失败")
            return []

        # 兼容两种返回结构
        payload = data.get("data") or {}
        items = payload.get("list") or payload.get("product_list") or []

        courses: List[Course] = []
        for item in items:
            try:
                course_info = item.get("course") or {}
                classroom_id = int(item.get("classroom_id", 0) or 0)
                if not classroom_id:
                    continue
                courses.append(
                    Course(
                        course_name=(
                            item.get("name")
                            or course_info.get("name")
                            or "未知课程"
                        ),
                        classroom_id=classroom_id,
                        course_sign=item.get("course_sign", "") or "",
                        sku_id=int(item.get("sku_id", 0) or 0),
                        course_id=int(
                            course_info.get("id")
                            or item.get("course_id", 0)
                            or 0
                        ),
                        university_id=self.university_id,
                    )
                )
            except (TypeError, ValueError) as exc:
                logger.debug("跳过异常课程项：%s (%s)", item, exc)
        logger.info("共获取到 %d 门课程", len(courses))
        return courses

    # ------------------------------------------------------------------
    def get_chapters(self, course: Course) -> List[Dict[str, Any]]:
        """获取课程章节原始结构。"""
        url = (
            f"{self.session.base_url}/mooc-api/v1/lms/learn/course/chapter"
            f"?cid={course.classroom_id}&term=latest"
            f"&uv_id={self.university_id}&sign={course.course_sign}"
        )
        headers = {"classroom-id": str(course.classroom_id), "Xt-Agent": "web"}
        data = self.session.get_json(url, headers=headers)
        if not data or not data.get("success", True):
            logger.error("获取章节失败：%s", course.course_name)
            return []
        return data.get("data", {}).get("course_chapter", []) or []

    # ------------------------------------------------------------------
    def list_leaves(
        self, course: Course, leaf_type: Optional[int] = None
    ) -> List[Leaf]:
        """列出课程中所有叶子节点，可按类型过滤。

        Args:
            course: 目标课程。
            leaf_type: 若指定，则只返回该类型的叶子节点。
        """
        chapters = self.get_chapters(course)
        leaves: List[Leaf] = []

        for chapter in chapters:
            chapter_name = chapter.get("name", "未知章节")
            for section in chapter.get("section_leaf_list", []) or []:
                # 情况一：section 下还有 leaf_list（嵌套结构）
                nested = section.get("leaf_list")
                if nested:
                    for leaf in nested:
                        parsed = self._parse_leaf(leaf, chapter_name, section)
                        if parsed:
                            leaves.append(parsed)
                else:
                    # 情况二：section 本身即为叶子节点
                    parsed = self._parse_leaf(section, chapter_name, section)
                    if parsed:
                        leaves.append(parsed)

        if leaf_type is not None:
            leaves = [leaf for leaf in leaves if leaf.leaf_type == leaf_type]

        logger.debug(
            "课程《%s》解析到 %d 个叶子节点（过滤类型=%s）",
            course.course_name, len(leaves), leaf_type,
        )
        return leaves

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_leaf(
        leaf: Dict[str, Any],
        chapter_name: str,
        section: Dict[str, Any],
    ) -> Optional[Leaf]:
        """将原始 leaf 字典解析为 ``Leaf`` 对象。"""
        leaf_id = leaf.get("id")
        if leaf_id is None:
            return None

        leaf_type = leaf.get("leaf_type")
        # leaf_type 为 None 时通常是视频（雨课堂历史遗留结构）
        if leaf_type is None:
            leaf_type = LEAF_VIDEO

        try:
            leaf_type = int(leaf_type)
        except (TypeError, ValueError):
            leaf_type = LEAF_VIDEO

        return Leaf(
            leaf_id=int(leaf_id),
            name=leaf.get("name") or section.get("name") or "未命名",
            leaf_type=leaf_type,
            chapter_name=chapter_name,
            section_id=int(section.get("id", 0) or 0),
            sku_id=int(leaf.get("sku_id") or section.get("sku_id") or 0),
            leafinfo_id=int(leaf.get("leafinfo_id") or 0),
            raw=leaf,
        )

    # ------------------------------------------------------------------
    def get_leaf_info(self, course: Course, leaf_id: int) -> Optional[Dict[str, Any]]:
        """获取单个叶子节点的详细信息（含视频 media 信息）。"""
        url = (
            f"{self.session.base_url}/mooc-api/v1/lms/learn/leaf_info/"
            f"{course.classroom_id}/{leaf_id}/"
        )
        headers = {"classroom-id": str(course.classroom_id), "Xt-Agent": "web"}
        data = self.session.get_json(url, headers=headers)
        if not data or not data.get("success"):
            logger.debug("获取 leaf_info 失败：leaf_id=%s", leaf_id)
            return None
        return data.get("data", {})

    # ------------------------------------------------------------------
    def get_leaf_progress(self, course: Course, leaf_id: int) -> Optional[Dict[str, Any]]:
        """获取视频叶子节点的观看进度。

        使用 ``/video-log/detail/`` 接口，返回 ``heartbeat`` 字段，
        其中包含 ``rate``（完成率）、``completed``、``last_point`` 等。
        """
        url = (
            f"{self.session.base_url}/video-log/detail/"
            f"?cid={course.course_id}&user_id={self.session.user_id}"
            f"&classroom_id={course.classroom_id}&video_id={leaf_id}"
            f"&video_type=video&term=latest&uv_id={self.university_id}"
        )
        headers = {"classroom-id": str(course.classroom_id), "Xt-Agent": "web"}
        data = self.session.get_json(url, headers=headers)
        if not data:
            return None
        return data.get("data", {})
