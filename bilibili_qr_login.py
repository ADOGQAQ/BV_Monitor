"""
Bilibili 二维码登录模块
通过扫码登录获取 SESSDATA
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode


logger = logging.getLogger(__name__)

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"


@dataclass
class QRCodeInfo:
    """二维码信息"""
    qrcode_key: str
    qr_image_url: str
    qr_image_data: bytes  # 二维码图片二进制数据


@dataclass
class LoginResult:
    """登录结果"""
    success: bool
    sessdata: Optional[str] = None
    message: str = ""
    # 状态码含义:
    # 0: 扫码成功
    # 86101: 未扫码
    # 86090: 已扫码未确认
    # 86038: 二维码已失效


class BilibiliQRLogin:
    """Bilibili 二维码登录客户端"""

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout
        self._qrcode_key: Optional[str] = None
        self._cancelled = False

    def generate_qrcode(self) -> QRCodeInfo:
        """生成登录二维码"""
        self._cancelled = False
        request = Request(
            QR_GENERATE_URL,
            headers={
                "Accept": "application/json",
                "Referer": "https://passport.bilibili.com/login",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"获取二维码失败: {exc}") from exc

        if not isinstance(payload, dict) or payload.get("code") != 0:
            msg = payload.get("message", "未知错误") if isinstance(payload, dict) else "响应格式错误"
            raise RuntimeError(f"获取二维码失败: {msg}")

        data = payload.get("data") or {}
        qrcode_key = data.get("qrcode_key", "")
        qr_url = data.get("url", "")

        if not qrcode_key or not qr_url:
            raise RuntimeError("获取二维码失败：返回数据不完整")

        self._qrcode_key = qrcode_key

        # 使用 qrcode 库生成二维码图片
        qr_image_data = self._generate_qr_image(qr_url)

        return QRCodeInfo(
            qrcode_key=qrcode_key,
            qr_image_url=qr_url,
            qr_image_data=qr_image_data,
        )

    @staticmethod
    def _generate_qr_image(content: str) -> bytes:
        """根据内容生成二维码图片（PNG格式）"""
        try:
            import qrcode
            from io import BytesIO

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=2,
            )
            qr.add_data(content)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            logger.warning("qrcode 库未安装，无法生成二维码图片")
            return b""
        except Exception as exc:
            logger.warning("生成二维码图片失败: %s", exc)
            return b""

    def poll_login_status(self) -> LoginResult:
        """轮询登录状态"""
        if not self._qrcode_key:
            return LoginResult(success=False, message="未生成二维码")

        params = {"qrcode_key": self._qrcode_key}
        url = f"{QR_POLL_URL}?{urlencode(params)}"

        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Referer": "https://passport.bilibili.com/login",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )

        try:
            with urlopen(request, timeout=self._timeout) as response:
                # 尝试从 Set-Cookie 中提取 SESSDATA
                set_cookie = response.headers.get("Set-Cookie", "")
                sessdata = self._extract_sessdata_from_cookie(set_cookie)

                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return LoginResult(success=False, message=f"HTTP {exc.code}")
        except (URLError, OSError, TimeoutError) as exc:
            return LoginResult(success=False, message=f"网络错误: {exc.reason}")
        except (UnicodeError, json.JSONDecodeError) as exc:
            return LoginResult(success=False, message=f"响应解析失败: {exc}")

        if not isinstance(payload, dict):
            return LoginResult(success=False, message="响应格式错误")

        # 外层 code=0 表示 API 调用成功，真正的登录状态在 data.code 中
        if payload.get("code") != 0:
            message = payload.get("message", "") or "API调用失败"
            return LoginResult(success=False, message=message)

        data = payload.get("data") or {}
        inner_code = data.get("code")
        inner_message = data.get("message", "")

        # data.code=0 表示登录成功
        if inner_code == 0:
            # 登录成功后，需要访问 data.url 来获取完整的 Cookie
            login_url = data.get("url", "")
            if not sessdata and login_url:
                sessdata = self._fetch_sessdata_from_login_url(login_url)

            if sessdata:
                return LoginResult(success=True, sessdata=sessdata, message="登录成功")
            else:
                # 登录成功但没拿到 SESSDATA
                return LoginResult(
                    success=True,
                    sessdata=None,
                    message="登录成功，但未能提取到 SESSDATA"
                )

        # 其他状态码
        status_messages = {
            86101: "请使用 B 站 APP 扫码登录",
            86090: "已扫码，请在手机上确认登录",
            86038: "二维码已失效，请刷新",
        }
        msg = status_messages.get(inner_code, inner_message or f"状态码: {inner_code}")
        return LoginResult(success=False, message=msg)

    @staticmethod
    def _extract_sessdata_from_cookie(cookie_str: str) -> Optional[str]:
        """从 Set-Cookie 字符串中提取 SESSDATA"""
        if not cookie_str:
            return None
        import re
        # 匹配 SESSDATA=xxx; 格式
        match = re.search(r'SESSDATA\s*=\s*([^;\s]+)', cookie_str, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _fetch_sessdata_from_login_url(self, login_url: str) -> Optional[str]:
        """访问登录成功后的 URL，从 Set-Cookie 中提取 SESSDATA"""
        if not login_url:
            return None
        try:
            import http.cookiejar
            from urllib.request import build_opener, HTTPCookieProcessor

            cookie_jar = http.cookiejar.CookieJar()
            opener = build_opener(HTTPCookieProcessor(cookie_jar))
            request = Request(
                login_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://passport.bilibili.com/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
            )
            with opener.open(request, timeout=self._timeout) as response:
                # 从 cookie jar 中查找 SESSDATA
                for cookie in cookie_jar:
                    if cookie.name == "SESSDATA":
                        return cookie.value
                # 也尝试从响应头中提取
                set_cookie = response.headers.get("Set-Cookie", "")
                return self._extract_sessdata_from_cookie(set_cookie)
        except Exception as exc:
            logger.warning("从登录 URL 获取 SESSDATA 失败: %s", exc)
            return None

    def cancel(self):
        """取消登录轮询"""
        self._cancelled = True

    def login_with_polling(
        self,
        on_status: Callable[[str], None],
        interval: float = 2.0,
        max_attempts: int = 90,  # 最多轮询 3 分钟
    ) -> LoginResult:
        """
        持续轮询登录状态（阻塞方法，应在子线程中调用）

        Args:
            on_status: 状态更新回调
            interval: 轮询间隔（秒）
            max_attempts: 最大轮询次数
        """
        for i in range(max_attempts):
            if self._cancelled:
                return LoginResult(success=False, message="已取消")

            result = self.poll_login_status()

            if result.success:
                on_status("✅ 登录成功！")
                return result

            on_status(result.message)

            # 如果二维码失效，直接返回
            if "失效" in result.message:
                return result

            time.sleep(interval)

        return LoginResult(success=False, message="登录超时，请重试")
