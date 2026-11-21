# -*- coding: utf-8 -*-
"""登录模块。

支持两种登录方式：
1. **扫码登录**：通过雨课堂 WebSocket 获取微信登录二维码，扫码后换取 Cookie。
2. **Cookie 登录**：直接使用 ``.env`` 中配置的 ``sessionid`` / ``csrftoken``。

学校 ID 需手动配置；登录成功后同步 ``user_id``。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Optional

from .config import Config
from .logger import get_logger, ok, step, warn
from .session import YuketangSession

logger = get_logger("login")


class LoginError(Exception):
    """登录失败异常。"""


# ======================================================================
#  扫码登录
# ======================================================================
class QrcodeLogin:
    """通过 WebSocket 获取微信登录二维码并等待扫码。"""

    def __init__(self, domain: str, timeout: int = 180, callback=None, stop_event=None) -> None:
        self.domain = domain
        self.timeout = timeout
        self.login_message: str = ""
        self._ws = None
        self._timer: Optional[threading.Timer] = None
        self._done = threading.Event()
        self.callback = callback
        self.stop_event = stop_event or threading.Event()
        self.error_message = ""

    # ------------------------------------------------------------------
    def _print_qrcode(self, qr_data: str) -> None:
        """生成二维码图片并尝试用系统查看器打开。"""
        if self.callback:
            self.callback(qr_data)
            return
        try:
            import qrcode
        except ImportError:
            logger.error("缺少 qrcode 依赖，请执行：pip install qrcode Pillow")
            print(f"\n请手动访问以下链接完成登录：\n{qr_data}\n")
            return

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        qr_file = os.path.join(tempfile.gettempdir(), "yuketang_qrcode.png")
        img.save(qr_file, format="PNG")

        print("\n" + "=" * 60)
        print("请使用【微信】扫描弹出的二维码完成登录")
        print(f"二维码文件：{qr_file}")
        print(f"扫码链接：{qr_data}")
        print("=" * 60 + "\n")

        try:
            if sys.platform == "darwin":
                subprocess.call(["open", qr_file])
            elif os.name == "nt":
                os.startfile(qr_file)  # type: ignore[attr-defined]
            else:
                subprocess.call(["xdg-open", qr_file])
        except Exception as exc:  # pragma: no cover
            warn(f"无法自动打开二维码，请手动打开：{qr_file}（{exc}）")

    # ------------------------------------------------------------------
    def _on_message(self, _ws, message: str) -> None:
        if self.stop_event.is_set():
            self._close()
            return
        try:
            msg = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return

        if msg.get("ticket") and msg.get("qrcode"):
            self._print_qrcode(msg["qrcode"])

        if msg.get("op") == "requestlogin":
            self._fetch_qrcode()

        if msg.get("op") == "loginsuccess":
            self.login_message = message
            self._done.set()
            self._close()

    def _on_error(self, _ws, error) -> None:
        import ssl
        from websocket import WebSocketTimeoutException
        status = getattr(error, "status_code", None)
        if isinstance(error, ssl.SSLError):
            self.error_message = "登录连接的证书校验失败，请检查系统时间和网络代理"
        elif status:
            self.error_message = f"登录连接被拒绝（HTTP {status}），请检查雨课堂站点和网络"
        elif isinstance(error, (TimeoutError, WebSocketTimeoutException)):
            self.error_message = "登录连接超时，请检查网络后重试"
        else:
            self.error_message = "无法连接扫码登录服务，请检查雨课堂站点和网络代理"
        logger.debug("WebSocket 连接异常：%s", type(error).__name__)

    def _on_close(self, _ws, _code, _msg) -> None:
        self._done.set()

    def _on_open(self, _ws) -> None:
        if self.stop_event.is_set():
            self._close()
            return
        logger.debug("WebSocket 已连接")
        self._fetch_qrcode()
        self._timer = threading.Timer(60, self._fetch_qrcode)
        self._timer.daemon = True
        self._timer.start()

    def _fetch_qrcode(self) -> None:
        if self._ws is not None:
            try:
                self._ws.send(
                    json.dumps(
                        {
                            "op": "requestlogin",
                            "role": "web",
                            "version": 1.4,
                            "type": "qrcode",
                        }
                    )
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("请求二维码失败：%s", exc)

    def _close(self) -> None:
        if self._timer:
            self._timer.cancel()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    def run(self) -> str:
        """阻塞等待扫码，返回登录成功消息（JSON 字符串）。"""
        try:
            import websocket
        except ImportError as exc:
            raise LoginError(
                "缺少 websocket-client 依赖，请执行：pip install websocket-client"
            ) from exc

        step("正在获取登录二维码…")
        self._ws = websocket.WebSocketApp(
            f"wss://{self.domain}/wsapp/",
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.on_open = self._on_open

        def connect():
            try:
                self._ws.run_forever(origin=f"https://{self.domain}")
            except Exception as exc:
                self._on_error(self._ws, exc)
            finally:
                self._done.set()

        thread = threading.Thread(target=connect, daemon=True)
        thread.start()

        deadline = time.monotonic() + self.timeout
        while not self._done.wait(0.2):
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                self._close()
                raise LoginError("登录已取消" if self.stop_event.is_set() else "扫码登录超时，请重试")

        if self.stop_event.is_set():
            raise LoginError("登录已取消")
        if not self.login_message:
            raise LoginError(self.error_message or "扫码登录连接已关闭，请重新获取二维码")
        return self.login_message


# ======================================================================
#  登录管理器
# ======================================================================
class LoginManager:
    """统一登录入口。"""

    def __init__(self, config: Config, qrcode_callback=None, stop_event=None) -> None:
        self.config = config
        self.qrcode_callback = qrcode_callback
        self.stop_event = stop_event

    # ------------------------------------------------------------------
    def login(self, force_qrcode: bool = False) -> YuketangSession:
        """执行登录并返回已认证的会话。

        Args:
            force_qrcode: 强制使用扫码登录，忽略已有 Cookie。
        """
        if not force_qrcode and self.config.has_credentials:
            step("检测到已配置 Cookie，尝试直接登录…")
            session = self._build_session(self.config.build_cookies())
            if self._verify(session):
                ok("Cookie 登录成功")
                return session
            warn("Cookie 已失效，转为扫码登录")

        return self._qrcode_login()

    # ------------------------------------------------------------------
    def _build_session(self, cookies: Dict[str, str]) -> YuketangSession:
        session = YuketangSession(
            domain=self.config.domain,
            cookies=cookies,
            university_id=self.config.effective_university_id,
            csrf_token=self.config.csrf_token,
            max_retries=self.config.max_retries,
        )
        if self.stop_event is not None:
            session.stop_event = self.stop_event
        return session

    # ------------------------------------------------------------------
    def _qrcode_login(self) -> YuketangSession:
        """扫码登录流程。"""
        university_id = self.config.effective_university_id
        if not university_id:
            raise LoginError("请先在设置 → 连接中手动填写学校 ID，再扫码登录")

        if self.stop_event is not None and self.stop_event.is_set():
            raise LoginError("登录已取消")
        login_message = QrcodeLogin(self.config.domain, callback=self.qrcode_callback,
                                   stop_event=self.stop_event).run()
        message = json.loads(login_message)
        auth_info = message.get("Auth")
        user_id = message.get("UserID")
        if not auth_info or not user_id:
            raise LoginError("扫码结果缺少认证信息")

        # 用扫码结果换取正式 Cookie
        verify_url = (
            f"{self.config.base_url}/edu_admin/account/login/"
            f"verify-origin-system-bind?term=latest&uv_id={university_id}"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Referer": f"{self.config.base_url}/pro/portal/home/",
            "Origin": self.config.base_url,
            "Cookie": (
                f"university_id={university_id};platform_id=3;"
                "xtbz=cloud;platform_type=1;"
            ),
            "Platform-Id": "3",
            "University-Id": str(university_id),
            "Terminal-Type": "web",
            "X-Client": "web",
            "X-Csrftoken": "undefined",
            "Xtbz": "cloud",
        }
        payload = {"auth": auth_info, "origin_user_id": str(user_id)}

        if self.stop_event is not None and self.stop_event.is_set():
            raise LoginError("登录已取消")

        import requests

        try:
            resp = requests.post(
                verify_url, json=payload, headers=headers, timeout=15
            )
        except requests.exceptions.RequestException:
            raise LoginError("登录验证连接失败，请检查网络后重试") from None
        if resp.status_code != 200:
            raise LoginError(f"登录验证失败（HTTP {resp.status_code}），请检查学校 ID 后重试")

        set_cookie = resp.headers.get("Set-Cookie", "")
        cookies = self._parse_set_cookie(set_cookie)
        if not cookies.get("sessionid"):
            raise LoginError("登录响应中未包含 sessionid，请重试")

        cookies.setdefault("university_id", str(university_id))
        cookies.setdefault("platform_id", "3")
        cookies.setdefault("xtbz", "ykt")

        # 回写配置，便于后续复用
        self.config.session_id = cookies.get("sessionid", "")
        self.config.csrf_token = cookies.get("csrftoken", "")
        self.config.university_id = str(university_id)
        self.config.user_id = str(user_id)

        session = self._build_session(cookies)
        if not self._verify(session):
            raise LoginError("扫码登录后校验失败，请重试")

        ok(f"扫码登录成功，欢迎 {user_id}")
        self._save_cookies(cookies)
        return session

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_set_cookie(raw: str) -> Dict[str, str]:
        """解析 Set-Cookie 头为字典。"""
        cookies: Dict[str, str] = {}
        for part in raw.split(","):
            for item in part.split(";"):
                item = item.strip()
                if "=" not in item:
                    continue
                key, _, value = item.partition("=")
                key = key.strip()
                if key in {"path", "domain", "expires", "max-age", "samesite", "httponly"}:
                    continue
                cookies[key] = value.strip()
        return cookies

    # ------------------------------------------------------------------
    def _verify(self, session: YuketangSession) -> bool:
        """校验会话是否有效，并同步 user_id。"""
        data = session.get_json(f"{session.base_url}/edu_admin/check_user_session/")
        if not data:
            return False
        # 该接口返回结构不固定，尝试多种方式提取 user_id
        text = json.dumps(data, ensure_ascii=False)
        match = re.search(r'"user_id"\s*:\s*"?(\d+)"?', text)
        if match:
            self.config.user_id = match.group(1)
            session.user_id = match.group(1)
            session.headers["User-Id"] = match.group(1)
            self.config.session_university_id = session.university_id
            return True
        # 部分部署返回 success 字段
        verified = bool(data.get("success") or data.get("data"))
        if verified:
            self.config.session_university_id = session.university_id
        return verified

    # ------------------------------------------------------------------
    def _save_cookies(self, cookies: Dict[str, str]) -> None:
        """将登录 Cookie 写回 ``.env``，方便下次直接复用。"""
        from pathlib import Path

        env_file = self.config.project_root / ".env"
        try:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
            mapping = {
                "SESSION_ID": cookies.get("sessionid", ""),
                "CSRF_TOKEN": cookies.get("csrftoken", ""),
                "UNIVERSITY_ID": cookies.get("university_id", ""),
                "SESSION_UNIVERSITY_ID": self.config.session_university_id,
            }
            updated = []
            seen = set()
            for line in lines:
                key = line.split("=", 1)[0].strip() if "=" in line else ""
                if key in mapping:
                    updated.append(f"{key}={mapping[key]}")
                    seen.add(key)
                else:
                    updated.append(line)
            for key, value in mapping.items():
                if key not in seen and value:
                    updated.append(f"{key}={value}")
            env_file.write_text("\n".join(updated) + "\n", encoding="utf-8")
            logger.debug("登录凭证已写回 .env")
        except Exception as exc:  # pragma: no cover
            logger.debug("写回 .env 失败：%s", exc)
