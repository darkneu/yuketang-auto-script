# -*- coding: utf-8 -*-
"""统一日志模块。

提供带颜色的控制台输出与文件日志，支持 ``rich`` 缺失时自动降级。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.logging import RichHandler

    _RICH = True
except ImportError:  # pragma: no cover
    _RICH = False

_LOGGER_NAME = "yuketang"
_configured = False

# 简易 ANSI 颜色（rich 不可用时使用）
_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """无 rich 时的彩色格式化器。"""

    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname:<8}{_RESET}"
        return super().format(record)


def setup_logger(
    log_file: Optional[str] = None,
    debug: bool = False,
    quiet: bool = False,
) -> logging.Logger:
    """初始化并返回全局 logger。

    Args:
        log_file: 日志文件路径，为 ``None`` 时仅输出到控制台。
        debug: 是否输出 DEBUG 级别日志。
        quiet: 是否静默控制台输出（仅写文件）。
    """
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        logger.setLevel(logging.DEBUG if debug else logging.INFO)
        return logger

    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    # ---- 控制台 ----
    if not quiet:
        if _RICH:
            console = Console(stderr=False)
            handler: logging.Handler = RichHandler(
                console=console,
                show_path=False,
                show_time=True,
                rich_tracebacks=True,
                markup=False,
                log_time_format="%H:%M:%S",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
        else:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                _ColorFormatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
            )
        handler.setLevel(logging.DEBUG if debug else logging.INFO)
        logger.addHandler(handler)

    # ---- 文件 ----
    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

    _configured = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取子 logger。"""
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)


# 便捷输出函数（供 CLI 使用）
def banner(text: str) -> None:
    """打印醒目的分节标题。"""
    logger = get_logger()
    line = "=" * 60
    logger.info(line)
    logger.info(text)
    logger.info(line)


def step(text: str) -> None:
    """打印步骤信息。"""
    get_logger().info("▶ %s", text)


def ok(text: str) -> None:
    """打印成功信息。"""
    get_logger().info("✅ %s", text)


def warn(text: str) -> None:
    """打印警告信息。"""
    get_logger().warning("⚠️  %s", text)


def fail(text: str) -> None:
    """打印失败信息。"""
    get_logger().error("❌ %s", text)
