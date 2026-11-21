"""Local display history. Never used to decide whether business work is skipped."""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import datetime, timezone


_RESULT_STATES = {
    "服务端已完成", "已处理", "已跳过", "未确认完成", "处理失败", "部分失败",
    "未处理", "未完成", "考试已锁定", "考试尚未开放", "考试已截止",
    "开放状态未确认", "题目获取失败", "缺少题目标识", "接口数据异常", "暂无题目", "已停止",
}
_OUTCOMES = {"success", "skipped", "failed", "unprocessed", "stopped"}


def _digest(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid_time(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def display_time(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def progress_caption(state):
    if "server_rate" in state or "server_completed" in state:
        rate = state.get("server_rate")
        return "服务端完成率：" + (f"{rate:.1%}" if rate is not None else
                               "已完成" if state.get("server_completed") is True else "未知")
    rate = state.get("cached_server_rate")
    if rate is not None or "cached_server_completed" in state:
        value = f"{rate:.1%}" if rate is not None else "已完成" if state.get("cached_server_completed") else "未完成"
        return f"上次服务端完成率：{value}（{display_time(state.get('cache_progress_at'))}；本次尚未核实）"
    return "服务端完成率：未知"


class TaskStateStore:
    """One small, atomic JSON file per account/course; only safe summary fields."""

    def __init__(self):
        self.path = None
        self.records = {}
        self.fingerprints = {}
        self.dirty = False
        self.failed = False

    def open_course(self, config, session, course, leaves):
        self.close()
        self.failed = False
        # A successful login without a known account ID cannot safely reuse history.
        if session is None or not session.user_id or not session.university_id:
            return {}
        account = _digest([session.domain.lower(), str(session.university_id), str(session.user_id)])
        course_key = _digest([course.classroom_id, course.course_id, course.sku_id])
        self.path = config.project_root / "task-state" / account / (course_key + ".json")
        self.fingerprints = {str(leaf.leaf_id): _digest([
            leaf.leaf_id, leaf.leaf_type, leaf.leafinfo_id, leaf.sku_id, leaf.name,
        ]) for leaf in leaves}
        try:
            if self.path.is_file():
                if self.path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("State file too large")
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("tasks"), dict):
                    raise ValueError("Invalid state file")
                for lid, fingerprint in self.fingerprints.items():
                    entry = saved["tasks"].get(lid)
                    if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
                        continue
                    clean = {"fingerprint": fingerprint}
                    result = entry.get("result")
                    if (isinstance(result, dict) and result.get("state") in _RESULT_STATES
                            and result.get("outcome") in _OUTCOMES and _valid_time(result.get("at"))):
                        clean["result"] = {key: result[key] for key in ("state", "outcome", "at")}
                    progress = entry.get("progress")
                    if isinstance(progress, dict) and _valid_time(progress.get("at")):
                        fields = self._progress_fields(progress)
                        if fields:
                            clean["progress"] = dict(fields, at=progress["at"])
                    if len(clean) > 1:
                        self.records[lid] = clean
        except (OSError, ValueError, TypeError, RecursionError):
            # Corrupt/unreadable history is never a reason to stop a course task.
            self.records = {}
        restored = {leaf.leaf_id: self.restore(leaf.leaf_id) for leaf in leaves}
        return {lid: state for lid, state in restored.items() if state}

    @staticmethod
    def _progress_fields(values):
        result = {}
        rate = values.get("server_rate")
        if isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 <= rate <= 1:
            if math.isfinite(rate):
                result["server_rate"] = rate
        if isinstance(values.get("server_completed"), bool):
            result["server_completed"] = values["server_completed"]
        return result

    def remember(self, leaf_id, values):
        lid = str(leaf_id)
        if self.path is None or lid not in self.fingerprints:
            return False
        progress = self._progress_fields(values)
        has_result = values.get("state") in _RESULT_STATES and values.get("outcome") in _OUTCOMES
        if not progress and not has_result:
            return False  # No disk work, serialization or question copying for UI deltas.
        entry = self.records.setdefault(lid, {"fingerprint": self.fingerprints[lid]})
        if progress:
            entry["progress"] = dict(progress, at=_timestamp())
            entry.pop("result", None)  # A fresh server snapshot supersedes an older run result.
        if has_result:
            entry["result"] = {"state": values["state"], "outcome": values["outcome"], "at": _timestamp()}
        self.dirty = True
        return True

    def restore(self, leaf_id):
        entry = self.records.get(str(leaf_id), {})
        result, progress = entry.get("result", {}), entry.get("progress", {})
        if not result and not progress:
            return {}
        state = {}
        if result:
            if result["state"] == "服务端已完成" and result["outcome"] in {"success", "skipped"}:
                caption = "上次已完成"
            elif result["outcome"] == "success":
                caption = "上次已处理"
            elif result["outcome"] == "skipped":
                caption = "上次已作答"
            elif result["outcome"] == "stopped":
                caption = "上次已停止"
            else:
                caption = "上次未完成"
            state.update(state=caption, cached_state=result["state"], cache_checked_at=result["at"])
        else:
            complete = progress.get("server_completed") is True or progress.get("server_rate", 0) >= 1
            state.update(state="上次已完成" if complete else "上次未完成", cache_checked_at=progress["at"])
        for key in ("server_rate", "server_completed"):
            if key in progress:
                state["cached_" + key] = progress[key]
        if progress:
            state["cache_progress_at"] = progress["at"]
        # Deliberately no live outcome/answer/position: history cannot count as this run's success.
        return state

    def flush(self):
        if not self.dirty or self.path is None:
            return True
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"version": 1, "tasks": self.records}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            self.failed = True
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        self.dirty = self.failed = False
        return True

    def close(self):
        self.flush()
        self.path = None
        self.records = {}
        self.fingerprints = {}
        self.dirty = False
