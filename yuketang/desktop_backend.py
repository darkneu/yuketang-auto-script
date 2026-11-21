"""Desktop adapter. Reuses existing requests and workers without background fetching."""
from __future__ import annotations

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from .ai import create_ai_client
from .config import Config
from .course import CourseAPI, LEAF_VIDEO, LEAF_RICHTEXT, LEAF_DISCUSSION, LEAF_HOMEWORK, LEAF_EXAM
from .homework import HomeworkWorker
from .login import LoginManager
from .richtext import RichtextWorker, DiscussionWorker
from .video import VideoWatcher


# Only functional, user-editable options. Credentials never enter a settings snapshot.
FIELDS = {
    "domain": "YUKETANG_DOMAIN", "university_id": "UNIVERSITY_ID",
    "ai_mode": "AI_MODE", "deepseek_base_url": "DEEPSEEK_BASE_URL",
    "deepseek_model": "DEEPSEEK_MODEL", "deepseek_temperature": "DEEPSEEK_TEMPERATURE",
    "deepseek_timeout": "DEEPSEEK_TIMEOUT", "codex_cli": "CODEX_CLI",
    "codex_model": "CODEX_MODEL", "codex_timeout": "CODEX_TIMEOUT",
    "auto_video": "AUTO_VIDEO",
    "video_speed": "VIDEO_SPEED", "heartbeat_interval": "HEARTBEAT_INTERVAL",
    "max_concurrent_videos": "MAX_CONCURRENT_VIDEOS", "skip_completed": "SKIP_COMPLETED",
    "max_retries": "MAX_RETRIES", "auto_richtext": "AUTO_RICHTEXT",
    "richtext_stay_seconds": "RICHTEXT_STAY_SECONDS", "auto_discussion": "AUTO_DISCUSSION",
    "auto_homework": "AUTO_HOMEWORK", "auto_exam": "AUTO_EXAM",
}
RANGES = {
    "video_speed": (0.25, 16), "heartbeat_interval": (5, 300),
    "max_concurrent_videos": (1, 8), "max_retries": (1, 10),
    "richtext_stay_seconds": (0, 600), "deepseek_temperature": (0, 2),
    "deepseek_timeout": (1, 3600), "codex_timeout": (1, 3600),
}


def settings_snapshot(config):
    return {key: getattr(config, key) for key in FIELDS}


def save_settings(config: Config, values: dict, path: Path, *, new_key="", clear_key=False):
    unknown = values.keys() - FIELDS.keys()
    if unknown:
        raise ValueError("未知设置项")
    cleaned = {}
    for key, value in values.items():
        default = getattr(Config(), key)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError("开关设置无效")
        elif isinstance(default, (float, int)):
            value = float(value)
            if not math.isfinite(value) or not RANGES[key][0] <= value <= RANGES[key][1]:
                raise ValueError("数值超出范围")
            if isinstance(default, int):
                if not value.is_integer():
                    raise ValueError("请输入整数")
                value = int(value)
        else:
            value = str(value).strip()
            if any(c in value for c in "\n\r\x00"):
                raise ValueError("设置不能包含换行")
        cleaned[key] = value
    candidate = replace(config, **cleaned)
    site_changed = (candidate.domain != config.domain or
                    bool(candidate.university_id) and candidate.university_id != config.effective_university_id)
    if site_changed:
        candidate.session_id = candidate.csrf_token = candidate.user_id = ""
        candidate.cookies = {}
        candidate.session_university_id = ""
    elif config.has_credentials:
        candidate.session_university_id = config.effective_university_id
    if candidate.ai_mode not in {"deepseek", "codex"}:
        raise ValueError("请选择 AI 模式")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d+)?", candidate.domain):
        raise ValueError("请输入有效站点域名")
    if candidate.ai_mode == "deepseek":
        url = urlsplit(candidate.deepseek_base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("请输入有效 API 地址")
        if not candidate.deepseek_model:
            raise ValueError("请输入 DeepSeek 模型")
    elif not candidate.codex_cli:
        raise ValueError("请输入 Codex 程序路径")
    new_key = new_key.strip()
    if any(c in new_key for c in "\n\r\x00"):
        raise ValueError("密钥格式无效")
    mapping = {FIELDS[key]: getattr(candidate, key) for key in cleaned}
    if site_changed:
        mapping.update(SESSION_ID="", CSRF_TOKEN="")
    if site_changed or candidate.session_university_id != config.session_university_id:
        mapping["SESSION_UNIVERSITY_ID"] = candidate.session_university_id
    if clear_key or new_key:
        candidate.deepseek_api_key = "" if clear_key else new_key
        mapping["DEEPSEEK_API_KEY"] = candidate.deepseek_api_key
    _write_mapping(path, mapping)
    return candidate


def _write_mapping(path, mapping):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    result, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ")
        if key in mapping:
            if key not in seen:
                result.append(f"{key}={_env_value(mapping[key])}")
                seen.add(key)
        else:
            result.append(line)
    result.extend(f"{key}={_env_value(value)}" for key, value in mapping.items() if key not in seen)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    temporary.replace(path)


def _env_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    # Quote spaces and preserve Windows paths using dotenv's single-quote escaping.
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


class DesktopService:
    def __init__(self, config=None, emit=None):
        self.config = config or Config.load()
        self.emit = emit or (lambda *_: None)
        self.cancel = threading.Event()
        self.session = None
        self.courses = []
        self.leaves = []
        self.course = None
        self._lock = threading.Lock()
        self.busy = False
        self._homework_worker = None
        self.ai_reply_observer = None

    def acquire(self):
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            self.cancel.clear()
            return True

    def release(self):
        with self._lock:
            self.busy = False

    def stop(self):
        self.cancel.set()

    def logout(self, persist=True):
        if self.busy:
            raise ValueError("请先停止任务")
        if persist:
            _write_mapping(self.config.project_root / ".env", {"SESSION_ID": "", "CSRF_TOKEN": "", "SESSION_UNIVERSITY_ID": ""})
        if self.session:
            self.session.session.cookies.clear()
            self.session.session.close()
        self.session = None
        self.course, self.leaves, self.courses = None, [], []
        self.config.session_id = self.config.csrf_token = self.config.user_id = ""
        self.config.session_university_id = ""
        self.config.cookies = {}

    def selected_leaves(self):
        enabled = {kind for key, kind in (("auto_video", LEAF_VIDEO), ("auto_richtext", LEAF_RICHTEXT),
                   ("auto_discussion", LEAF_DISCUSSION), ("auto_homework", LEAF_HOMEWORK), ("auto_exam", LEAF_EXAM))
                   if getattr(self.config, key)}
        leaves = [leaf for leaf in self.leaves if leaf.leaf_type in enabled]
        return leaves[:self.config.test_task_count] if self.config.test_mode else leaves

    def run_course_isolated(self):
        if self.session is None or self.course is None:
            raise ValueError("请先选择课程")
        if not self.selected_leaves():
            raise ValueError("请先选择需要处理的任务类型")
        from .desktop_runtime import run_isolated
        snapshot = {"domain": self.session.domain, "cookies": self.session.session.cookies.get_dict(),
                    "university_id": self.session.university_id, "csrf_token": self.session.csrf_token,
                    "user_id": self.session.user_id, "max_retries": self.session.max_retries,
                    "timeout": self.session.timeout}
        run_isolated("course", self.config, self.cancel, self.emit,
                     session_data=snapshot, course=self.course, leaves=self.leaves)

    def login(self, force_qrcode=False):
        manager = LoginManager(self.config, lambda qr: self.emit("qr", qr), self.cancel)
        session = manager.login(force_qrcode=force_qrcode)
        if self.cancel.is_set():
            session.session.close()
            return
        if self.session:
            self.session.session.close()
        self.session = session
        self.session.stop_event = self.cancel
        self.session.observer = lambda leaf_id, values: self.emit("task", (leaf_id, values))
        # Same list request as CLI. No per-course or per-task fetching here.
        self.courses = CourseAPI(self.session, self.config.effective_university_id).list_courses()
        if not self.cancel.is_set():
            self.emit("courses", self.courses)

    def open_course(self, course):
        if self.session is None:
            raise ValueError("请先登录")
        leaves = CourseAPI(self.session, self.config.effective_university_id).list_leaves(course)
        if self.cancel.is_set():
            return
        self.course, self.leaves = course, leaves
        self.emit("leaves", (course, leaves))

    def run_course(self):
        if self.session is None or self.course is None:
            raise ValueError("请先选择课程")
        leaves = self.selected_leaves()
        # Match CLI worker lifetime: retain the current font across this course run.
        self._homework_worker = None
        ai = None
        if any(leaf.leaf_type in {LEAF_DISCUSSION, LEAF_HOMEWORK, LEAF_EXAM} for leaf in leaves):
            problems = self.config.ai_problems()
            if problems:
                raise ValueError("请在设置中补全所选 AI 模式的连接配置")
            ai = create_ai_client(self.config)
            ai.stop_event = self.cancel
            ai.reply_observer = self.ai_reply_observer
        for leaf in leaves:
            self.emit("task", (leaf.leaf_id, {"state": "等待中"}))
        videos = [leaf for leaf in leaves if leaf.leaf_type == LEAF_VIDEO]
        # Guard inside each worker prevents queued work from fetching after cancellation.
        with ThreadPoolExecutor(max_workers=self.config.max_concurrent_videos) as pool:
            futures = [pool.submit(self._run_one, leaf, ai) for leaf in videos]
            for future in futures:
                future.result()
        for leaf in leaves:
            if self.cancel.is_set():
                break
            if leaf.leaf_type != LEAF_VIDEO:
                self._run_one(leaf, ai)
        if self.cancel.is_set():
            self.emit("cancelled", None)
        else:
            self.emit("run_done", len(leaves))

    def _run_one(self, leaf, ai):
        if self.cancel.is_set():
            return
        self.emit("task", (leaf.leaf_id, {"state": "处理中"}))
        reason = ""
        try:
            if leaf.leaf_type == LEAF_VIDEO:
                watcher = VideoWatcher(self.session, self.config)
                result = watcher._watch_one(self.course, leaf, None, None, self.config.skip_completed, None)
                state = {"success": "服务端已完成", "skipped": "服务端已完成", "failed": "未确认完成"}[result]
            elif leaf.leaf_type == LEAF_RICHTEXT:
                state = "已处理" if RichtextWorker(self.session, self.config).finish_one(self.course, leaf) else "处理失败"
            elif leaf.leaf_type == LEAF_DISCUSSION:
                state = "已处理" if DiscussionWorker(self.session, self.config, ai).post_one(self.course, leaf) else "处理失败"
            else:
                if self._homework_worker is None:
                    self._homework_worker = HomeworkWorker(self.session, self.config, ai)
                stats = self._homework_worker.solve_leaf(self.course, leaf)
                reason = stats.get("reason", "")
                unavailable = {
                    "locked": "考试已锁定", "not_open": "考试尚未开放", "closed": "考试已截止",
                    "access_unknown": "开放状态未确认", "fetch_failed": "题目获取失败",
                    "missing_ids": "缺少题目标识", "invalid_data": "接口数据异常", "empty": "暂无题目",
                    "unprocessed": "未处理",
                }
                state = (unavailable.get(stats.get("status"), "未完成") if stats.get("status") else
                         "未处理" if not stats["total"] else
                         "已跳过" if stats.get("skipped") == stats["total"] else
                         "部分失败" if stats["failed"] else
                         "已处理" if stats["success"] == stats["total"] else "未完成")
        except Exception as exc:
            state = "处理失败"
            # Exception text can include URLs/credentials; expose its type without raw payloads.
            reason = f"任务执行异常（{type(exc).__name__}），本项未确认完成，可刷新课程后重试"
        if self.cancel.is_set():
            state = "已停止"
        outcome = ("stopped" if self.cancel.is_set() else
                   "skipped" if state == "已跳过" else
                   "success" if state in {"已处理", "服务端已完成"} else
                   "unprocessed" if state in {"未处理", "未完成", "考试已锁定", "考试尚未开放", "考试已截止", "开放状态未确认", "暂无题目"} else "failed")
        if leaf.leaf_type == LEAF_VIDEO and not self.cancel.is_set() and state == "服务端已完成" and result == "skipped":
            outcome = "skipped"
        values = {"state": state, "stage": "", "outcome": outcome}
        if reason:
            values["reason"] = reason
        self.emit("task", (leaf.leaf_id, values))
