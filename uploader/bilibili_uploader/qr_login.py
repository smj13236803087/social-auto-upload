"""Bilibili QR login for Web UI (no interactive terminal / biliup TTY)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import segno

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
TV_AUTH_CODE_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
TV_CONFIRM_URL = "https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/confirm"
TV_POLL_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"

# biliup BiliTV app key (used to exchange web cookies → access_token)
BILITV_APPKEY = "4409e2ce8ffd12b8"
BILITV_APPSEC = "59b43e04ad6965f34319062b478f83dd"

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


def _sign(params: dict, appsec: str = BILITV_APPSEC) -> str:
    encoded = urlencode(dict(sorted(params.items())))
    return hashlib.md5(f"{encoded}{appsec}".encode("utf-8")).hexdigest()


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
                "http_only": 1
                if c.has_nonstandard_attr("HttpOnly")
                or getattr(c, "_rest", {}).get("HttpOnly") is not None
                else 0,
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
        "platform": None,
    }


def _cookie_map_from_login_info(login_info: dict) -> dict[str, str]:
    cookies = ((login_info.get("cookie_info") or {}).get("cookies")) or []
    return {
        str(item.get("name") or ""): str(item.get("value") or "")
        for item in cookies
        if item.get("name")
    }


def exchange_web_cookies_for_bilitv_tokens(login_info: dict) -> dict:
    """Use SESSDATA to auto-confirm a BiliTV QR and obtain access_token for biliup."""
    cookie_map = _cookie_map_from_login_info(login_info)
    sessdata = cookie_map.get("SESSDATA") or ""
    bili_jct = cookie_map.get("bili_jct") or ""
    if not sessdata or not bili_jct:
        raise RuntimeError("缺少 SESSDATA/bili_jct，无法换取 B 站上传凭证")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:38.0) Gecko/20100101 Firefox/38.0 Iceweasel/38.2.1 BiliApp",
        }
    )

    form = {
        "appkey": BILITV_APPKEY,
        "local_id": "0",
        "ts": str(int(time.time())),
    }
    form["sign"] = _sign(form)
    auth_resp = session.post(TV_AUTH_CODE_URL, data=form, timeout=20)
    auth_resp.raise_for_status()
    auth_body = auth_resp.json() or {}
    if auth_body.get("code") != 0:
        raise RuntimeError(auth_body.get("message") or "获取 BiliTV 二维码失败")
    auth_code = str((auth_body.get("data") or {}).get("auth_code") or "").strip()
    if not auth_code:
        raise RuntimeError("BiliTV 未返回 auth_code")

    confirm = session.post(
        TV_CONFIRM_URL,
        headers={
            "Cookie": f"SESSDATA={sessdata}; bili_jct={bili_jct}",
            "User-Agent": session.headers["User-Agent"],
        },
        data={
            "auth_code": auth_code,
            "csrf": bili_jct,
            "scanning_type": 3,
        },
        timeout=20,
    )
    confirm.raise_for_status()
    confirm_body = confirm.json() or {}
    if confirm_body.get("code") != 0:
        raise RuntimeError(
            confirm_body.get("message")
            or f"自动确认 B 站凭证失败（code={confirm_body.get('code')}），请重新扫码登录"
        )

    poll_form = {
        "appkey": BILITV_APPKEY,
        "auth_code": auth_code,
        "local_id": "0",
        "ts": str(int(time.time())),
    }
    poll_form["sign"] = _sign(poll_form)
    deadline = time.time() + 30
    while time.time() < deadline:
        poll = session.post(TV_POLL_URL, data=poll_form, timeout=20)
        poll.raise_for_status()
        body = poll.json() or {}
        code = body.get("code")
        if code == 0 and isinstance(body.get("data"), dict):
            data = body["data"]
            # biliup LoginInfo shape
            token_info = data.get("token_info") or {}
            if not token_info.get("access_token"):
                raise RuntimeError("换取 access_token 失败，请重新扫码登录 B 站")
            result = {
                "cookie_info": data.get("cookie_info") or login_info.get("cookie_info"),
                "sso": data.get("sso")
                or [
                    "https://passport.bilibili.com/api/v2/oauth2/access_token",
                ],
                "token_info": {
                    "access_token": token_info.get("access_token") or "",
                    "expires_in": int(token_info.get("expires_in") or 0),
                    "mid": int(token_info.get("mid") or 0),
                    "refresh_token": token_info.get("refresh_token") or "",
                },
                "platform": "BiliTV",
            }
            return result
        if code == 86039:
            time.sleep(1)
            continue
        raise RuntimeError(body.get("message") or f"换取上传凭证失败（code={code}）")
    raise RuntimeError("换取 B 站上传凭证超时，请重试")


def ensure_biliup_login_info(account_file: str | Path) -> dict:
    """Ensure cookie JSON has access_token; upgrade from web cookies if needed."""
    path = Path(account_file)
    if not path.is_file():
        raise RuntimeError(f"B站账号文件不存在: {path}")
    login_info = json.loads(path.read_text(encoding="utf-8"))
    token = ((login_info.get("token_info") or {}).get("access_token") or "").strip()
    if token:
        return login_info
    upgraded = exchange_web_cookies_for_bilitv_tokens(login_info)
    path.write_text(json.dumps(upgraded, ensure_ascii=False, indent=2), encoding="utf-8")
    return upgraded


def bilibili_qr_login(
    account_file: str | Path,
    *,
    qrcode_callback: Callable[[dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
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
        if cancel_check and cancel_check():
            return {
                "success": False,
                "status": "cancelled",
                "message": "登录已取消",
                "account_file": str(account_path),
                "qrcode": qrcode_info,
            }
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
            # 网页 Cookie 不够 biliup 上传用，换成带 access_token 的 BiliTV 凭证
            try:
                login_info = exchange_web_cookies_for_bilitv_tokens(login_info)
            except Exception as exc:  # noqa: BLE001 - surface as login failure
                raise RuntimeError(f"扫码成功，但换取上传凭证失败：{exc}") from exc
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
