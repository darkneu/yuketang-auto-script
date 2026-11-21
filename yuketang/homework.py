# -*- coding: utf-8 -*-
"""作业与考试自动答题模块。

流程：
1. 获取题目列表 ``get_exercise_list``。
2. 解密加密字体题干。
3. 解析题型（选择/判断/填空/简答）。
4. 按配置调用 DeepSeek / Codex 生成答案。
5. 提交答案 ``problem_apply``。

支持作业（leaf_type=6）与考试（leaf_type=5）。
"""

from __future__ import annotations

import re
import math
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
    AIClient,
)
from .config import Config
from .course import LEAF_EXAM, LEAF_HOMEWORK, Course, CourseAPI, Leaf
from .decrypt import FontDecryptor, load_mapping_from_file
from .logger import get_logger, ok, warn
from .session import YuketangSession

logger = get_logger("homework")


class ExerciseUnavailable(Exception):
    """A safe, user-facing reason why no further answers may be submitted."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class HomeworkWorker:
    """作业 / 考试自动答题器。"""

    def __init__(
        self,
        session: YuketangSession,
        config: Config,
        ai: Optional[AIClient] = None,
    ) -> None:
        self.session = session
        self.config = config
        self.ai = ai
        self.course_api = CourseAPI(session, config.effective_university_id)
        self.decryptor = FontDecryptor(
            load_mapping_from_file(str(config.project_root / "data" / "font_mapping.json")),
            cache_dir=str(config.project_root / "data" / "font_cache"),
        )
        # 已加载过的字体 URL，避免重复下载
        self._loaded_font_url: Optional[str] = None
        self._exam_sources = ()

    @staticmethod
    def _locked(value: Any) -> Optional[bool]:
        # JSON booleans and their numeric/text encodings have the same meaning.
        # Unknown encodings must never silently grant access.
        if isinstance(value, str):
            value = value.strip().lower()
            if value in {"false", "0"}:
                return False
            if value in {"true", "1"}:
                return True
            return None
        if isinstance(value, (bool, int, float)) and value in (0, 1):
            return bool(value)
        return None

    def _check_exam_access(self, leaf: Leaf, *sources: dict) -> None:
        if leaf.leaf_type != LEAF_EXAM:
            return
        now = time.time()
        for source in (leaf.raw or {}, *sources):
            if "is_locked" in source and source["is_locked"] is not None:
                locked = self._locked(source["is_locked"])
                if locked is None:
                    raise ExerciseUnavailable("access_unknown", "考试锁定状态无法识别，请刷新课程后重试")
                if locked:
                    raise ExerciseUnavailable("locked", "考试已锁定或尚未开放，请在平台确认开放状态")
            for field, status, reason in (
                ("start_time", "not_open", "考试尚未到开放时间"),
                ("end_time", "closed", "考试已截止，未继续答题或提交"),
            ):
                value = source.get(field)
                if value is None or value == "" or value == 0 or value == "0":
                    continue
                try:
                    stamp = float(value)
                    if isinstance(value, bool) or not math.isfinite(stamp) or stamp < 0:
                        raise ValueError
                    # Chapter/leaf metadata normally uses milliseconds; also accept seconds.
                    stamp = stamp / 1000 if stamp >= 100_000_000_000 else stamp
                except (TypeError, ValueError, OverflowError):
                    raise ExerciseUnavailable("access_unknown", "考试开放时间格式无法识别，请在平台确认") from None
                if (field == "start_time" and now < stamp) or (field == "end_time" and now >= stamp):
                    raise ExerciseUnavailable(status, reason)

    @staticmethod
    def _unavailable_result(exc: ExerciseUnavailable, stats=None):
        warn(exc.reason)
        result = dict(stats) if stats is not None else {"total": 0, "success": 0, "failed": 0}
        result.update(status=exc.status, reason=exc.reason)
        return result

    # ==================================================================
    #  题目获取
    # ==================================================================
    def get_problems(self, course: Course, leaf: Leaf) -> Optional[Dict[str, Any]]:
        """获取题目列表原始数据。

        正确接口为 ``/mooc-api/v1/lms/exercise/get_exercise_list/{exercise_id}/{sku_id}/``，
        其中 ``exercise_id`` 取自 ``leaf_info.content_info.leaf_type_id``，
        ``sku_id`` 取自 ``leaf_info.sku_id``（回退到课程 sku_id）。
        """
        self._exam_sources = ()
        if self.session.stop_event.is_set():
            return None
        self._check_exam_access(leaf)
        self.session.emit(leaf.leaf_id, stage="获取任务详情")
        info = self.course_api.get_leaf_info(course, leaf.leaf_id)
        if self.session.stop_event.is_set():
            return None
        if not isinstance(info, dict):
            raise ExerciseUnavailable("fetch_failed", "任务详情获取失败，请检查登录状态或稍后重试")
        content_info = info.get("content_info")
        if content_info is None:
            content_info = {}
        if not isinstance(content_info, dict):
            raise ExerciseUnavailable("invalid_data", "任务详情格式异常，未获取或提交题目")
        self._exam_sources = (info, content_info)
        self._check_exam_access(leaf, *self._exam_sources)
        exercise_id = content_info.get("leaf_type_id") or leaf.leafinfo_id
        sku_id = info.get("sku_id") or leaf.sku_id or course.sku_id
        if not exercise_id or not sku_id:
            raise ExerciseUnavailable("missing_ids", "任务详情缺少 exercise_id/sku_id，无法获取题目")

        url = (
            f"{self.session.base_url}/mooc-api/v1/lms/exercise/get_exercise_list/"
            f"{exercise_id}/{sku_id}/?term=latest&uv_id={self.config.effective_university_id}"
        )
        headers = {"classroom-id": str(course.classroom_id), "Xtbz": "ykt"}
        self.session.emit(leaf.leaf_id, stage="获取题目")
        data = self.session.get_json(url, headers=headers)
        if self.session.stop_event.is_set():
            return None
        if not isinstance(data, dict) or not data.get("success"):
            raise ExerciseUnavailable("fetch_failed", "题目接口未返回成功，请检查考试开放状态、登录状态或稍后重试")
        payload = data.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("problems"), list):
            raise ExerciseUnavailable("invalid_data", "题目接口返回格式异常，未继续答题或提交")
        self._exam_sources += (payload,)
        self._check_exam_access(leaf, *self._exam_sources)
        self.session.emit(leaf.leaf_id, stage="")
        return payload

    def _prepare_font(self, font_url: Optional[str]) -> None:
        """Only load a font when an unanswered problem needs parsing."""
        if font_url != self._loaded_font_url:
            # A failed/new/missing font must never reuse another exercise's mapping.
            self.decryptor.clear()
            self._loaded_font_url = None
        if font_url and font_url != self._loaded_font_url:
            logger.info("加载加密字体：%s", font_url)
            if self.decryptor.load_from_url(font_url):
                self._loaded_font_url = font_url
                logger.info("字体解密就绪，共 %d 个字符", len(self.decryptor.char_map))
            else:
                warn("加密字体加载失败，题干可能显示为乱码")

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
            warn(f"{self.config.ai_label} 未就绪，无法自动答题，请检查对应模式配置")
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
        if self.session.stop_event.is_set():
            return False
        url = (
            f"{self.session.base_url}/mooc-api/v1/lms/exercise/problem_apply/"
            f"?term=latest&uv_id={self.config.effective_university_id}"
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
    def solve_leaf(self, course: Course, leaf: Leaf) -> Dict[str, Any]:
        """完成单个作业 / 考试。"""
        self._exam_sources = ()
        try:
            data = self.get_problems(course, leaf)
        except ExerciseUnavailable as exc:
            return self._unavailable_result(exc)
        if not data:
            return self._unavailable_result(ExerciseUnavailable("unprocessed", "未取得题目，未执行答题或提交"))

        problems = data.get("problems", []) or []
        if not problems:
            logger.info("《%s》没有题目", leaf.name)
            return self._unavailable_result(ExerciseUnavailable("empty", "接口返回空题目列表，未执行答题或提交"))

        if self.config.test_mode:
            problems = problems[: self.config.test_task_count]

        logger.info("《%s》共 %d 道题", leaf.name, len(problems))
        stats = {"total": len(problems), "success": 0, "failed": 0}
        skipped = 0
        font_prepared = False

        for index, problem in enumerate(problems, 1):
            if self.session.stop_event.is_set():
                break
            try:
                self._check_exam_access(leaf, *self._exam_sources)
            except ExerciseUnavailable as exc:
                return self._unavailable_result(exc, dict(stats, skipped=skipped))
            user = problem.get("user", {}) or {}
            if user.get("my_count") and user.get("count") and user["my_count"] >= user["count"]:
                logger.info("⏭️  [%d/%d] 已作答，跳过", index, len(problems))
                stats["success"] += 1
                skipped += 1
                # No decryption or HTML parsing solely to populate history.
                self.session.emit(leaf.leaf_id, problem={
                    "id": problem.get("problem_id"),
                    "type": (problem.get("content") or {}).get("Type", ""),
                    "state": "已作答 · 跳过",
                })
                continue
            if not font_prepared:
                self._prepare_font(data.get("font"))
                font_prepared = True
            if self.session.stop_event.is_set():
                break
            parsed = self._parse_problem(problem)
            if not parsed:
                stats["failed"] += 1
                continue

            def report(state, answer=None, *, include_content=False):
                item = {"id": parsed["problem_id"], "state": state}
                if include_content:
                    item.update(type=parsed["type"], body=parsed["body"],
                                options=parsed["options"], blank_count=parsed["blank_count"])
                if answer is not None:
                    item["answer"] = answer
                self.session.emit(leaf.leaf_id, problem=item)

            logger.info(
                "── [%d/%d] [%s] %s",
                index, len(problems), parsed["type"], parsed["body"][:60],
            )

            report("生成答案中", include_content=True)
            answer = self._solve(parsed)
            if self.session.stop_event.is_set():
                report("已停止")
                break
            if answer is None:
                stats["failed"] += 1
                report("生成失败")
                continue

            try:
                self._check_exam_access(leaf, *self._exam_sources)
            except ExerciseUnavailable as exc:
                report("未提交 · " + exc.reason)
                return self._unavailable_result(exc, dict(stats, skipped=skipped))

            logger.info("   AI 答案：%s", answer)
            report("提交中", answer)

            if self.config.homework_confirm and not self.config.test_mode:
                # 非交互模式下默认提交；如需人工确认可在此扩展
                pass

            if self._submit(course, parsed["problem_id"], answer, parsed["type"]):
                ok("提交成功")
                stats["success"] += 1
                report("已提交", answer)
            else:
                warn("提交失败")
                stats["failed"] += 1
                report("提交失败", answer)

            if self.session.stop_event.wait(1):
                break

        logger.info(
            "《%s》完成：成功 %d / 失败 %d（共 %d）",
            leaf.name, stats["success"], stats["failed"], stats["total"],
        )
        stats["skipped"] = skipped
        return stats

    # ------------------------------------------------------------------
    def solve_course(self, course: Course, include_exam: bool = False) -> Dict[str, int]:
        """完成课程中所有作业（可选含考试）。"""
        all_leaves = self.course_api.list_leaves(course)
        leaves = [leaf for leaf in all_leaves if leaf.leaf_type == LEAF_HOMEWORK]
        if include_exam:
            leaves += [leaf for leaf in all_leaves if leaf.leaf_type == LEAF_EXAM]

        if not leaves:
            logger.info("课程《%s》中没有作业", course.course_name)
            return {"total": 0, "success": 0, "failed": 0}

        logger.info("课程《%s》共 %d 个作业/考试", course.course_name, len(leaves))
        total = {"total": 0, "success": 0, "failed": 0}
        for index, leaf in enumerate(leaves, 1):
            if self.session.stop_event.is_set():
                break
            logger.info("══ [%d/%d] %s", index, len(leaves), leaf.name)
            stats = self.solve_leaf(course, leaf)
            for key in total:
                total[key] += stats.get(key, 0)
            # Retain CLI pacing for attempted work, with no delay for completed/empty leaves.
            if stats["total"] > stats.get("skipped", 0) and index < len(leaves):
                if self.session.stop_event.wait(2):
                    break

        logger.info(
            "作业完成：成功 %d / 失败 %d（共 %d）",
            total["success"], total["failed"], total["total"],
        )
        return total
