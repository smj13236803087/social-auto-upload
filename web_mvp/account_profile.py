"""Resolve and cache real platform display names for bound accounts."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests

from conf import BASE_DIR

CACHE_PATH = Path(BASE_DIR) / "data" / "account_profiles.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600


def _cache_key(platform: str, account: str) -> str:
    return f"{platform}|{account}"


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _storage_session(account_file: Path) -> requests.Session:
    data = json.loads(account_file.read_text(encoding="utf-8"))
    session = requests.Session()
    for cookie in data.get("cookies") or []:
        name = cookie.get("name")
        if not name:
            continue
        session.cookies.set(
            name,
            cookie.get("value") or "",
            domain=cookie.get("domain") or None,
            path=cookie.get("path") or "/",
        )
    return session


def _fetch_douyin(account_file: Path) -> dict:
    session = _storage_session(account_file)
    resp = session.get(
        "https://creator.douyin.com/web/api/media/user/info/",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://creator.douyin.com/",
        },
        timeout=20,
    )
    resp.raise_for_status()
    user = (resp.json() or {}).get("user") or {}
    nickname = str(user.get("nickname") or "").strip()
    unique_id = str(user.get("unique_id") or user.get("short_id") or "").strip()
    uid = str(user.get("uid") or "").strip()
    if not nickname:
        raise RuntimeError("抖音未返回昵称")
    return {
        "display_name": nickname,
        "platform_id": unique_id or uid,
        "extra": {"unique_id": unique_id, "uid": uid},
    }


def _fetch_bilibili(account_file: Path) -> dict:
    data = json.loads(account_file.read_text(encoding="utf-8"))
    cookies = {
        c.get("name"): c.get("value")
        for c in (data.get("cookie_info") or {}).get("cookies") or []
        if c.get("name")
    }
    mid = str((data.get("token_info") or {}).get("mid") or cookies.get("DedeUserID") or "").strip()
    resp = requests.get(
        "https://api.bilibili.com/x/web-interface/nav",
        cookies=cookies,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("message") or "B站导航接口失败")
    info = payload.get("data") or {}
    uname = str(info.get("uname") or "").strip()
    mid = str(info.get("mid") or mid).strip()
    if not uname:
        raise RuntimeError("B站未返回用户名")
    return {
        "display_name": uname,
        "platform_id": mid,
        "extra": {"mid": mid},
    }


def _fetch_kuaishou(account_file: Path) -> dict:
    data = json.loads(account_file.read_text(encoding="utf-8"))
    user_id = ""
    for cookie in data.get("cookies") or []:
        if cookie.get("name") == "userId" and cookie.get("value"):
            user_id = str(cookie["value"])
            break

    # Prefer lightweight Playwright scrape of creator console.
    try:
        import asyncio

        from patchright.async_api import async_playwright

        async def _scrape() -> str:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True, channel="chromium")
                try:
                    context = await browser.new_context(storage_state=str(account_file))
                    page = await context.new_page()
                    await page.goto(
                        "https://cp.kuaishou.com/profile",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    await page.wait_for_timeout(3500)
                    loc = page.locator(".user-name").first
                    if await loc.count():
                        text = (await loc.inner_text()).strip()
                        if text:
                            return text
                finally:
                    await browser.close()
            return ""

        nickname = asyncio.run(_scrape())
        if nickname:
            return {
                "display_name": nickname,
                "platform_id": user_id,
                "extra": {"userId": user_id},
            }
    except Exception:
        pass

    if user_id:
        return {
            "display_name": f"快手用户{user_id}",
            "platform_id": user_id,
            "extra": {"userId": user_id},
        }
    raise RuntimeError("无法获取快手昵称")


def _fetch_tencent(account_file: Path) -> dict:
    """Scrape WeChat Channels creator console for display name."""
    import asyncio

    from patchright.async_api import async_playwright

    async def _scrape() -> tuple[str, str]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel="chrome")
            try:
                context = await browser.new_context(storage_state=str(account_file))
                page = await context.new_page()
                await page.goto(
                    "https://channels.weixin.qq.com/platform",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                await page.wait_for_timeout(3500)
                if "login" in (page.url or "").lower():
                    raise RuntimeError("视频号 Cookie 已失效")

                selectors = [
                    ".finder-nickname",
                    ".account-info .nickname",
                    ".video-account-info .name",
                    ".header-account-name",
                    '[class*="nickname"]',
                    ".account-name",
                ]
                for sel in selectors:
                    loc = page.locator(sel).first
                    try:
                        if await loc.count():
                            text = (await loc.inner_text()).strip()
                            if text and len(text) < 80:
                                return text, ""
                    except Exception:
                        continue

                # Fallback: page title often contains account name
                title = (await page.title() or "").strip()
                for junk in ("视频号助手", "微信", "-", "|", "·"):
                    title = title.replace(junk, " ")
                title = " ".join(title.split()).strip()
                if title:
                    return title, ""
                return "", ""
            finally:
                await browser.close()

    nickname, platform_id = asyncio.run(_scrape())
    if not nickname:
        raise RuntimeError("无法获取视频号昵称")
    return {
        "display_name": nickname,
        "platform_id": platform_id,
        "extra": {},
    }


def fetch_platform_profile(platform: str, account_file: Path) -> dict:
    if platform == "douyin":
        return _fetch_douyin(account_file)
    if platform == "bilibili":
        return _fetch_bilibili(account_file)
    if platform == "kuaishou":
        return _fetch_kuaishou(account_file)
    if platform == "tencent":
        return _fetch_tencent(account_file)
    raise ValueError(f"不支持的平台: {platform}")


def get_cached_profile(platform: str, account: str) -> dict | None:
    cache = _load_cache()
    item = cache.get(_cache_key(platform, account))
    if not item:
        return None
    updated = float(item.get("updated_ts") or 0)
    if updated and (time.time() - updated) > CACHE_TTL_SECONDS:
        return None
    return item


def resolve_account_profile(
    platform: str,
    account: str,
    account_file: Path,
    *,
    force: bool = False,
) -> dict:
    """Return profile dict with display_name; falls back to local account name."""
    if not force:
        cached = get_cached_profile(platform, account)
        if cached and cached.get("display_name"):
            return cached

    profile = {
        "display_name": account,
        "platform_id": "",
        "extra": {},
        "source": "local",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "updated_ts": time.time(),
        "error": "",
    }
    try:
        fetched = fetch_platform_profile(platform, account_file)
        profile.update(fetched)
        profile["source"] = "platform"
        profile["error"] = ""
    except Exception as exc:
        cached = _load_cache().get(_cache_key(platform, account)) or {}
        if cached.get("display_name"):
            profile = dict(cached)
            profile["error"] = str(exc)[:200]
        else:
            profile["error"] = str(exc)[:200]

    cache = _load_cache()
    cache[_cache_key(platform, account)] = profile
    _save_cache(cache)
    return profile
