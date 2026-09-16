"""Bilibili QR login for Web UI (no interactive terminal / biliup TTY)."""

from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests
import segno

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Referer": "https://passport.bilibili.com/login",
        "Origin": "https://passport.bilibili.com",
    }


def _qr_data_url(content: str) -> str:
    qr = segno.make(content, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=2)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _merge_cookies_from_redirect(session: requests.Session, redirect_url: str) -> None:
    """B 站成功时 cookies 常在 data.url 查询参数里，不一定都走 Set-Cookie。"""
    if not redirect_url:
        return
    qs = parse_qs(urlparse(redirect_url).query)
    domain = ".bilibili.com"
    for name in ("DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct"):
        values = qs.get(name) or []
        if values and values[0]:
            session.cookies.set(name, values[0], domain=domain, path="/")


def _cookies_to_login_info(session: requests.Session, refresh_token: str = "") -> dict:
    jar = session.cookies
    cookies: list[dict] = []
    for c in jar:
        cookies.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain or ".bilibili.com",
                "path": c.path or "/",
                "expires": int(c.expires) if c.expires else 0,
                "http_only": 1 if c.has_nonstandard_attr("HttpOnly") or getattr(c, "_rest", {}).get("HttpOnly") is not None else 0,
                "secure": 1 if c.secure else 0,
            }
        )
    by_name = {c["name"]: c["value"] for c in cookies}
    mid = 0
    try:
        mid = int(by_name.get("DedeUserID") or 0)
    except (TypeError, ValueError):
        mid = 0
    return {
        "cookie_info": {
            "cookies": cookies,
            "domains": [".bilibili.com", "bilibili.com", ".biligame.com", ".bigfun.cn"],
        },
        "sso": [
            "https://passport.bilibili.com/api/v2/oauth2/access_token",
        ],
        "token_info": {
            "access_token": "",
            "expires_in": 0,
            "mid": mid,
            "refresh_token": refresh_token or "",
        },
    }


def bilibili_qr_login(
    account_file: str | Path,
    *,
    qrcode_callback: Callable[[dict], None] | None = None,
    poll_interval: float = 2.0,
    timeout_seconds: float = 180.0,
) -> dict:
    """Generate QR → poll until scanned → write biliup-compatible cookie JSON."""
    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(_headers())

    gen = session.get(GENERATE_URL, params={"source": "main-fe-header"}, timeout=20)
    gen.raise_for_status()
    payload = gen.json() or {}
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("message") or "获取 B 站登录二维码失败")
    data = payload.get("data") or {}
    qrcode_key = str(data.get("qrcode_key") or "").strip()
    qr_url = str(data.get("url") or "").strip()
    if not qrcode_key or not qr_url:
        raise RuntimeError("B 站未返回二维码内容")

    image_data_url = _qr_data_url(qr_url)
    qrcode_info = {
        "image_data_url": image_data_url,
        "image_path": "",
        "qrcode_key": qrcode_key,
        "url": qr_url,
    }
    if qrcode_callback:
        qrcode_callback(qrcode_info)

    deadline = time.time() + max(30.0, float(timeout_seconds))
    last_status = ""
    while time.time() < deadline:
        poll = session.get(
            POLL_URL,
            params={"qrcode_key": qrcode_key, "source": "main-fe-header"},
            timeout=20,
        )
        poll.raise_for_status()
        body = poll.json() or {}
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or "轮询 B 站登录状态失败")
        info = body.get("data") or {}
        raw_status = info.get("code")
        status = int(raw_status) if raw_status is not None else -1
        # 0 success, 86101 waiting, 86090 scanned, 86038 expired
        if status == 0:
            _merge_cookies_from_redirect(session, str(info.get("url") or ""))
            refresh_token = str(info.get("refresh_token") or "")
            login_info = _cookies_to_login_info(session, refresh_token=refresh_token)
            if not any(c.get("name") == "SESSDATA" for c in login_info["cookie_info"]["cookies"]):
                raise RuntimeError("扫码成功但未拿到 SESSDATA，请重试")
            account_path.write_text(
                json.dumps(login_info, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if not account_path.is_file():
                raise RuntimeError("登录成功但 Cookie 文件未写入，请重试")
            mid = login_info.get("token_info", {}).get("mid") or ""
            return {
                "success": True,
                "status": "success",
                "message": "B站扫码登录成功",
                "account_file": str(account_path),
                "platform": "bilibili",
                "mid": mid,
                "qrcode": qrcode_info,
            }
        if status == 86038:
            raise RuntimeError("二维码已过期，请重新点击扫码登录")
        if status == 86090 and last_status != "scanned":
            last_status = "scanned"
            if qrcode_callback:
                qrcode_callback({**qrcode_info, "hint": "已扫码，请在手机上确认登录"})
        time.sleep(poll_interval)

    return {
        "success": False,
        "status": "timeout",
        "message": "等待 B 站扫码登录超时，请重试",
        "account_file": str(account_path),
        "qrcode": qrcode_info,
    }
