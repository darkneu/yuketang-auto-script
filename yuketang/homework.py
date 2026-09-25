# -*- coding: utf-8 -*-
"""作业与考试自动答题模块。

流程：
1. 获取题目列表 ``get_exercise_list``。
2. 解密加密字体题干。
3. 解析题型（选择/判断/填空/简答）。
4. 调用 DeepSeek 生成答案。
5. 提交答案 ``problem_apply``。

支持作业（leaf_type=6）与考试（leaf_type=5）。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from .ai import (
    TYPE_CHOICE,
    TYPE_FILL_BLANK,
    TYPE_JUDGEMENT,
    TYPE_MULTI_CHOICE,
    TYPE_SHORT_ANSWER,
    TYPE_SINGLE_CHOICE,
    AIError,
    DeepSeekClient,
)
from .config import Config
from .course import LEAF_EXAM, LEAF_HOMEWORK, Course, CourseAPI, Leaf
from .decrypt import FontDecryptor, load_mapping_from_file
from .logger import get_logger, ok, warn
from .session import YuketangSession

logger = get_logger("homework")


class HomeworkWorker:
    """作业 / 考试自动答题器。"""

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
        self.decryptor = FontDecryptor(
            load_mapping_from_file(str(config.project_root / "data" / "font_mapping.json")),
            cache_dir=str(config.project_root / "data" / "font_cache"),
        )
        # 已加载过的字体 URL，避免重复下载
        self._loaded_font_url: Optional[str] = None

    # ==================================================================
    #  题目获取
    # ==================================================================
    def get_problems(self, course: Course, leaf: Leaf) -> Optional[Dict[str, Any]]:
        """获取题目列表原始数据。

        正确接口为 ``/mooc-api/v1/lms/exercise/get_exercise_list/{exercise_id}/{sku_id}/``，
        其中 ``exercise_id`` 取自 ``leaf_info.content_info.leaf_type_id``，
        ``sku_id`` 取自 ``leaf_info.sku_id``（回退到课程 sku_id）。
        """
        info = self.course_api.get_leaf_info(course, leaf.leaf_id) or {}
        content_info = info.get("content_info", {}) or {}
        exercise_id = content_info.get("leaf_type_id") or leaf.leafinfo_id
        sku_id = info.get("sku_id") or leaf.sku_id or course.sku_id
        if not exercise_id or not sku_id:
            warn(f"缺少 exercise_id/sku_id，无法获取题目：{leaf.name}")
            return None

        # 考试未开放 / 已锁定：直接提示，避免无意义的接口请求
        raw = leaf.raw or {}
        if leaf.leaf_type == LEAF_EXAM and raw.get("is_locked"):
            start_ms = raw.get("start_time") or 0
            start_txt = ""
            if start_ms:
                try:
                    start_txt = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(start_ms / 1000)
                    )
                except (ValueError, OSError):
                    start_txt = ""
            tip = f"（开放时间：{start_txt}）" if start_txt else ""
            warn(f"考试尚未开放，跳过：{leaf.name}{tip}")
            return None

        url = (
            f"{self.session.base_url}/mooc-api/v1/lms/exercise/get_exercise_list/"
            f"{exercise_id}/{sku_id}/?term=latest&uv_id={self.config.university_id}"
        )
        headers = {"classroom-id": str(course.classroom_id), "Xtbz": "ykt"}
        data = self.session.get_json(url, headers=headers)
        if not data or not data.get("success"):
            warn(f"获取题目失败：{leaf.name}")
            return None
        payload = data.get("data", {}) or {}

        # 加载加密字体（每个作业/考试可能使用不同字体）
        font_url = payload.get("font")
        if font_url and font_url != self._loaded_font_url:
            logger.info("加载加密字体：%s", font_url)
            if self.decryptor.load_from_url(font_url):
                self._loaded_font_url = font_url
                logger.info("字体解密就绪，共 %d 个字符", len(self.decryptor.char_map))
            else:
                warn("加密字体加载失败，题干可能显示为乱码")

        return payload

    # ==================================================================
    #  题干解析
    # ==================================================================
    def _parse_problem(self, problem: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """解析单道题目。"""
        content = problem.get("content", {}) or {}
        problem_type = content.get("Type", "")
        body_html = content.get("Body", "") or ""

        # 解密加密字体
        if self.decryptor.loaded:
            body_html = self.decryptor.decrypt_html(body_html)

        body_text = self._html_to_text(body_html)

        options: List[str] = []
        option_map: Dict[str, str] = {}
        for option in content.get("Options", []) or []:
            key = option.get("key", "")
            value = option.get("value", "")
            if self.decryptor.loaded:
                value = self.decryptor.decrypt_html(value)
            value_text = self._html_to_text(value)
            options.append(f"{key}. {value_text}")
            option_map[key] = value_text

        # 填空题空数
        blank_count = self._count_blanks(body_html, content)

        return {
            "problem_id": problem.get("problem_id"),
            "type": problem_type,
            "body": body_text,
            "options": options,
            "option_map": option_map,
            "blank_count": blank_count,
            "user": problem.get("user", {}) or {},
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _html_to_text(html: str) -> str:
        if not html:
            return ""
        try:
            return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        except Exception:  # pragma: no cover
            return re.sub(r"<[^>]+>", "", html)

    # ------------------------------------------------------------------
    @staticmethod
    def _count_blanks(body_html: str, content: Dict[str, Any]) -> int:
        """统计填空题空数。"""
        # 优先使用接口提供的空数
        for key in ("BlankCount", "blank_count", "blankCount"):
            value = content.get(key)
            if value:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        # 回退：统计占位符
        count = len(re.findall(r"\[\[blank\]\]|＿{2,}|_{3,}", body_html or ""))
        return max(count, 1)

    # ==================================================================
    #  答案生成
    # ==================================================================
    def _solve(self, parsed: Dict[str, Any]) -> Optional[Any]:
        """调用 AI 生成答案。"""
        if not self.ai or not self.ai.available:
            warn("未配置 DeepSeek，无法自动答题")
            return None

        ptype = parsed["type"]
        try:
            if ptype in (TYPE_CHOICE, TYPE_SINGLE_CHOICE, TYPE_MULTI_CHOICE):
                return self.ai.answer(
                    ptype, parsed["body"], options=parsed["options"]
                )
            if ptype == TYPE_JUDGEMENT:
                return self.ai.answer(TYPE_JUDGEMENT, parsed["body"])
            if ptype == TYPE_FILL_BLANK:
                return self.ai.answer(
                    TYPE_FILL_BLANK, parsed["body"], blank_count=parsed["blank_count"]
                )
            return self.ai.answer(TYPE_SHORT_ANSWER, parsed["body"])
        except AIError as exc:
            logger.error("AI 答题失败：%s", exc)
            return None

    # ==================================================================
    #  答案提交
    # ==================================================================
    def _submit(self, course: Course, problem_id: Any, answer: Any, ptype: str) -> bool:
        """提交单题答案。"""
        url = (
            f"{self.session.base_url}/mooc-api/v1/lms/exercise/problem_apply/"
            f"?term=latest&uv_id={self.config.university_id}"
        )
        headers = {"classroom-id": str(course.classroom_id), "Xtbz": "ykt"}

        payload: Dict[str, Any] = {
            "classroom_id": course.classroom_id,
            "problem_id": problem_id,
        }
        if ptype == TYPE_FILL_BLANK and isinstance(answer, dict):
            payload["answers"] = answer
        elif ptype in (TYPE_CHOICE, TYPE_SINGLE_CHOICE, TYPE_MULTI_CHOICE):
            # 选择题：服务器要求无分隔符的选项字母字符串，如 "ABCD"
            if isinstance(answer, str):
                letters = re.findall(r"[A-Za-z]", answer)
            elif isinstance(answer, (list, tuple)):
                letters = [str(x).strip() for x in answer if str(x).strip()]
            else:
                letters = [str(answer)]
            payload["answer"] = "".join(letters).upper()
        else:
            payload["answer"] = answer

        result = self.session.post_json(url, payload=payload, headers=headers)
        if result and result.get("success"):
            return True
        logger.debug("提交答案响应：%s", result)
        return False

    # ==================================================================
    #  主流程
    # ==================================================================
    def solve_leaf(self, course: Course, leaf: Leaf) -> Dict[str, int]:
        """完成单个作业 / 考试。"""
        data = self.get_problems(course, leaf)
        if not data:
            return {"total": 0, "success": 0, "failed": 0}

        problems = data.get("problems", []) or []
        if not problems:
            logger.info("《%s》没有题目", leaf.name)
            return {"total": 0, "success": 0, "failed": 0}

        if self.config.test_mode:
            problems = problems[: self.config.test_task_count]

        logger.info("《%s》共 %d 道题", leaf.name, len(problems))
        stats = {"total": len(problems), "success": 0, "failed": 0}

        for index, problem in enumerate(problems, 1):
            parsed = self._parse_problem(problem)
            if not parsed:
                stats["failed"] += 1
                continue

            user = parsed["user"]
            # 已作答则跳过
            if user.get("my_count") and user.get("count") and user.get("my_count") >= user.get("count"):
                logger.info("⏭️  [%d/%d] 已作答，跳过", index, len(problems))
                stats["success"] += 1
                continue

            logger.info(
                "── [%d/%d] [%s] %s",
                index, len(problems), parsed["type"], parsed["body"][:60],
            )

            answer = self._solve(parsed)
            if answer is None:
                stats["failed"] += 1
                continue

            logger.info("   AI 答案：%s", answer)

            if self.config.homework_confirm and not self.config.test_mode:
                # 非交互模式下默认提交；如需人工确认可在此扩展
                pass

            if self._submit(course, parsed["problem_id"], answer, parsed["type"]):
                ok("提交成功")
                stats["success"] += 1
            else:
                warn("提交失败")
                stats["failed"] += 1

            time.sleep(1)

        logger.info(
            "《%s》完成：成功 %d / 失败 %d（共 %d）",
            leaf.name, stats["success"], stats["failed"], stats["total"],
        )
        return stats

    # ------------------------------------------------------------------
    def solve_course(self, course: Course, include_exam: bool = False) -> Dict[str, int]:
        """完成课程中所有作业（可选含考试）。"""
        leaves = self.course_api.list_leaves(course, leaf_type=LEAF_HOMEWORK)
        if include_exam:
            leaves += self.course_api.list_leaves(course, leaf_type=LEAF_EXAM)

        if not leaves:
            logger.info("课程《%s》中没有作业", course.course_name)
            return {"total": 0, "success": 0, "failed": 0}

        logger.info("课程《%s》共 %d 个作业/考试", course.course_name, len(leaves))
        total = {"total": 0, "success": 0, "failed": 0}
        for index, leaf in enumerate(leaves, 1):
            logger.info("══ [%d/%d] %s", index, len(leaves), leaf.name)
            stats = self.solve_leaf(course, leaf)
            for key in total:
                total[key] += stats.get(key, 0)
            time.sleep(2)

        logger.info(
            "作业完成：成功 %d / 失败 %d（共 %d）",
            total["success"], total["failed"], total["total"],
        )
        return total
