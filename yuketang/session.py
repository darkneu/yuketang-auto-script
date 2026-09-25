# -*- coding: utf-8 -*-
"""HTTP 会话封装。

在 ``requests.Session`` 基础上提供：
- 统一的请求头（含雨课堂所需的 ``xtbz`` / ``university-id`` 等）
- 自动重试与指数退避
- 雨课堂特有的「网络阻塞」限流响应处理
- 统一的 JSON 解析与错误日志
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Dict, Optional

import requests

from .logger import get_logger

logger = get_logger("session")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 雨课堂限流提示，例如 "Expected available in 3.5 seconds."
_BLOCK_PATTERN = re.compile(r"Expected available in\s*([\d.]+)\s*second", re.I)


class YuketangSession:
    """带重试与限流处理的雨课堂会话。"""

    def __init__(
        self,
        domain: str,
        cookies: Optional[Dict[str, str]] = None,
        university_id: str = "",
        csrf_token: str = "",
        max_retries: int = 3,
        timeout: int = 15,
        user_id: str = "",
    ) -> None:
        self.domain = domain
        self.base_url = f"https://{domain}"
        self.university_id = str(university_id or "")
        self.csrf_token = csrf_token or ""
        self.user_id = str(user_id or "")
        self.max_retries = max(1, max_retries)
        self.timeout = timeout

        self.session = requests.Session()
        if cookies:
            self.session.cookies.update(cookies)

        self.headers: Dict[str, str] = {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
            "X-Client": "web",
            "Xtbz": "ykt",
            "Terminal-Type": "web",
            "Platform-Id": "3",
            "University-Id": self.university_id,
            "X-CSRFToken": self.csrf_token,
        }

    # ------------------------------------------------------------------
    def update_headers(self, **kwargs: Any) -> None:
        """批量更新请求头。"""
        for key, value in kwargs.items():
            if value is None:
                continue
            # 允许传入 python 风格名（classroom_id -> classroom-id）
            header_key = key.replace("_", "-")
            self.headers[header_key] = str(value)

    def set_classroom(self, classroom_id: Any) -> None:
        """设置当前课堂上下文请求头。"""
        self.headers["classroom-id"] = str(classroom_id)

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        retry: bool = True,
        **kwargs: Any,
    ) -> Optional[requests.Response]:
        """发送请求，自动重试并处理限流。

        Returns:
            成功时返回 ``Response``，全部重试失败返回 ``None``。
        """
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"

        headers = kwargs.pop("headers", None)
        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)

        kwargs.setdefault("timeout", self.timeout)
        attempts = self.max_retries if retry else 1

        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(
                    method, url, headers=merged_headers, **kwargs
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                logger.warning(
                    "请求失败(%s %s) 第 %d/%d 次重试：%s",
                    method, url, attempt, attempts, exc,
                )
                if attempt < attempts:
                    time.sleep(min(2 ** attempt, 10) + random.uniform(0, 1))
                    continue
                logger.error("请求最终失败：%s %s", method, url)
                return None
            except requests.exceptions.RequestException as exc:
                logger.error("请求异常：%s %s -> %s", method, url, exc)
                return None

            # 处理雨课堂限流
            if resp.status_code in (429, 403) or "Expected available in" in resp.text:
                match = _BLOCK_PATTERN.search(resp.text)
                delay = float(match.group(1)) + 1 if match else 5.0
                logger.warning("触发雨课堂限流，等待 %.1f 秒后重试…", delay)
                if attempt < attempts:
                    time.sleep(delay)
                    continue
                logger.error("限流重试次数耗尽：%s", url)
                return resp

            if resp.status_code >= 500 and attempt < attempts:
                logger.warning(
                    "服务端错误 %d，第 %d/%d 次重试：%s",
                    resp.status_code, attempt, attempts, url,
                )
                time.sleep(min(2 ** attempt, 10))
                continue

            return resp

        return None

    # ------------------------------------------------------------------
    def get(self, url: str, **kwargs: Any) -> Optional[requests.Response]:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Optional[requests.Response]:
        return self.request("POST", url, **kwargs)

    # ------------------------------------------------------------------
    def get_json(self, url: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """GET 并解析 JSON，失败返回 ``None``。"""
        resp = self.get(url, **kwargs)
        return self._parse_json(resp, url)

    def post_json(
        self, url: str, payload: Any = None, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """POST 并解析 JSON，失败返回 ``None``。"""
        if payload is not None and "json" not in kwargs and "data" not in kwargs:
            kwargs["json"] = payload
        resp = self.post(url, **kwargs)
        return self._parse_json(resp, url)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(
        resp: Optional[requests.Response], url: str
    ) -> Optional[Dict[str, Any]]:
        if resp is None:
            return None
        if resp.status_code != 200:
            logger.debug("非 200 响应 %s -> %s", url, resp.status_code)
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.debug("响应非 JSON：%s -> %s", url, resp.text[:200])
            return None
