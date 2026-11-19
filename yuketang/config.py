# -*- coding: utf-8 -*-
"""全局配置管理。

从 ``.env`` 文件与环境变量中读取配置，提供类型安全的访问接口。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - 依赖缺失时降级
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Optional[str], default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """运行期配置对象。"""

    # ---------- 站点 ----------
    domain: str = "changjiang.yuketang.cn"
    university_id: str = ""

    # ---------- 登录凭证 ----------
    csrf_token: str = ""
    session_id: str = ""

    # ---------- AI 模式 ----------
    ai_mode: str = "deepseek"

    # ---------- DeepSeek（API） ----------
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_temperature: float = 0.3
    deepseek_timeout: int = 300

    # ---------- Codex（复用本机登录） ----------
    codex_cli: str = "codex"
    codex_model: str = ""
    codex_timeout: int = 300

    # ---------- 视频 ----------
    video_speed: float = 1.5
    heartbeat_interval: int = 15
    max_concurrent_videos: int = 2
    skip_completed: bool = True
    max_retries: int = 3

    # ---------- 图文 / 讨论 ----------
    auto_richtext: bool = True
    richtext_stay_seconds: int = 3
    auto_discussion: bool = False

    # ---------- 作业 / 考试 ----------
    auto_homework: bool = False
    auto_exam: bool = False
    homework_confirm: bool = True

    # ---------- 运行 ----------
    test_mode: bool = False
    test_task_count: int = 3
    debug: bool = False
    log_file: str = "logs/yuketang.log"

    # ---------- 运行时状态 ----------
    user_id: str = ""
    cookies: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        """站点根地址。"""
        return f"https://{self.domain}"

    @property
    def project_root(self) -> Path:
        """项目根目录。"""
        return PROJECT_ROOT

    @property
    def has_credentials(self) -> bool:
        """是否已具备可用的登录凭证。"""
        return bool(self.session_id)

    @property
    def has_codex(self) -> bool:
        """是否能找到 Codex CLI（登录状态由实际调用检查）。"""
        return bool(shutil.which(self.codex_cli))

    @property
    def has_deepseek(self) -> bool:
        """是否已配置 DeepSeek API Key。"""
        return bool(self.deepseek_api_key)

    @property
    def ai_label(self) -> str:
        return {"codex": "Codex", "deepseek": "DeepSeek"}.get(self.ai_mode, self.ai_mode)

    def ai_problems(self) -> list:
        """只检查选中模式，避免要求同时配置两个后端。"""
        if self.ai_mode == "codex":
            return [] if self.has_codex else ["未找到 Codex CLI，请安装或设置 CODEX_CLI"]
        if self.ai_mode == "deepseek":
            problems = []
            if not self.has_deepseek:
                problems.append("DEEPSEEK_API_KEY 未配置")
            if not self.deepseek_base_url:
                problems.append("DEEPSEEK_BASE_URL 未配置")
            if not self.deepseek_model:
                problems.append("DEEPSEEK_MODEL 未配置")
            return problems
        return ["AI_MODE 无效，只能设置为 codex 或 deepseek"]

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env_file: Optional[str] = None) -> "Config":
        """从 ``.env`` 与环境变量加载配置。"""
        env_path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
        else:
            load_dotenv(override=False)

        cfg = cls(
            domain=os.getenv("YUKETANG_DOMAIN", "changjiang.yuketang.cn").strip(),
            university_id=os.getenv("UNIVERSITY_ID", "").strip(),
            csrf_token=os.getenv("CSRF_TOKEN", "").strip(),
            session_id=os.getenv("SESSION_ID", "").strip(),
            ai_mode=os.getenv("AI_MODE", "deepseek").strip().lower(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip(),
            deepseek_temperature=_as_float(os.getenv("DEEPSEEK_TEMPERATURE"), 0.3),
            deepseek_timeout=max(1, _as_int(os.getenv("DEEPSEEK_TIMEOUT"), 300)),
            codex_cli=os.getenv("CODEX_CLI", "codex").strip() or "codex",
            codex_model=os.getenv("CODEX_MODEL", "").strip(),
            codex_timeout=max(1, _as_int(os.getenv("CODEX_TIMEOUT"), 300)),
            video_speed=_as_float(os.getenv("VIDEO_SPEED"), 1.5),
            heartbeat_interval=_as_int(os.getenv("HEARTBEAT_INTERVAL"), 15),
            max_concurrent_videos=_as_int(os.getenv("MAX_CONCURRENT_VIDEOS"), 2),
            skip_completed=_as_bool(os.getenv("SKIP_COMPLETED"), True),
            max_retries=_as_int(os.getenv("MAX_RETRIES"), 3),
            auto_richtext=_as_bool(os.getenv("AUTO_RICHTEXT"), True),
            richtext_stay_seconds=_as_int(os.getenv("RICHTEXT_STAY_SECONDS"), 3),
            auto_discussion=_as_bool(os.getenv("AUTO_DISCUSSION"), False),
            auto_homework=_as_bool(os.getenv("AUTO_HOMEWORK"), False),
            auto_exam=_as_bool(os.getenv("AUTO_EXAM"), False),
            homework_confirm=_as_bool(os.getenv("HOMEWORK_CONFIRM"), True),
            test_mode=_as_bool(os.getenv("TEST_MODE"), False),
            test_task_count=_as_int(os.getenv("TEST_TASK_COUNT"), 3),
            debug=_as_bool(os.getenv("DEBUG"), False),
            log_file=os.getenv("LOG_FILE", "logs/yuketang.log").strip(),
        )
        return cfg

    # ------------------------------------------------------------------
    def build_cookies(self) -> dict:
        """根据配置构造 Cookie 字典。"""
        cookies = {}
        if self.csrf_token:
            cookies["csrftoken"] = self.csrf_token
        if self.session_id:
            cookies["sessionid"] = self.session_id
        if self.university_id:
            cookies["university_id"] = self.university_id
        cookies.setdefault("platform_id", "3")
        cookies.setdefault("platform_type", "1")
        cookies.setdefault("xtbz", "ykt")
        return cookies

    def validate(self) -> list:
        """返回配置问题列表（空列表表示配置完整）。"""
        problems = []
        if not self.domain:
            problems.append("YUKETANG_DOMAIN 未配置")
        if not self.university_id:
            problems.append("UNIVERSITY_ID 未配置（可扫码登录后自动获取）")
        if not self.has_credentials:
            problems.append("SESSION_ID 未配置（将尝试扫码登录）")
        problems.extend(self.ai_problems())
        return problems
