"""Download Douyin / Kuaishou / Bilibili videos from share URLs via yt-dlp."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from conf import BASE_DIR

SUPPORTED_PLATFORMS = ("douyin", "kuaishou", "bilibili")

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_DOUYIN_SEC_UID_RE = re.compile(r"(?:/user/|/share/user/)(MS4wLjABAAAA[\w-]+)")
_DOUYIN_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


@dataclass(slots=True)
class DownloadResult:
    download_id: str
    platform: str
    source_url: str
    file_path: Path
    title: str
    filename: str


def _yt_dlp_cmd() -> list[str]:
    # Prefer the active interpreter's yt-dlp module (works under `uv run`).
    return [sys.executable, "-m", "yt_dlp"]


def extract_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("请粘贴分享链接")
    match = _URL_RE.search(text)
    if not match:
        raise ValueError("未识别到有效的 http(s) 链接")
    return match.group(0).rstrip(")/]}>.,，。")


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "douyin"
    if "kuaishou.com" in host or "chenzhongtech.com" in host or "gifshow.com" in host:
        return "kuaishou"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    raise ValueError("仅支持抖音 / 快手 / B站视频或主页链接")


@dataclass(slots=True)
class LatestVideoEntry:
    video_id: str
    url: str
    title: str
    platform: str


def _extract_bilibili_mid(url: str) -> str | None:
    match = re.search(r"space\.bilibili\.com/(\d+)", url)
    return match.group(1) if match else None


def _follow_redirects(url: str, *, timeout: int = 20) -> str:
    """Best-effort resolve short links; returns final or last Location URL."""
    import urllib.error
    import urllib.request

    current = url
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    req = urllib.request.Request(
        current,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            )
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.geturl() or current
    except urllib.error.HTTPError as exc:
        loc = exc.headers.get("Location") if exc.headers else None
        return loc or current
    except (urllib.error.URLError, TimeoutError):
        return current


def _extract_douyin_sec_uid(url: str) -> str | None:
    match = _DOUYIN_SEC_UID_RE.search(url)
    if match:
        return match.group(1)
    qs = parse_qs(urlparse(url).query)
    for key in ("sec_uid", "sec_user_id"):
        vals = qs.get(key) or []
        if vals and str(vals[0]).startswith("MS4wLjABAAAA"):
            return str(vals[0])
    return None


def _fetch_douyin_latest_via_browser(sec_uid: str) -> LatestVideoEntry | None:
    """Open douyin.com/user/<sec_uid> with bound Cookie and pick newest video id."""
    import asyncio

    cookie_json = _find_platform_cookie_json("douyin")
    if not cookie_json:
        return None

    async def _run() -> LatestVideoEntry | None:
        from patchright.async_api import async_playwright

        profile_url = f"https://www.douyin.com/user/{sec_uid}"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                storage_state=str(cookie_json),
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(4500)
                hrefs = await page.eval_on_selector_all(
                    'a[href*="/video/"]',
                    "els => els.map(e => e.href)",
                )
            finally:
                await browser.close()

        best_id = ""
        best_url = ""
        for href in hrefs or []:
            m = _DOUYIN_VIDEO_ID_RE.search(str(href))
            if not m:
                continue
            vid = m.group(1)
            # Douyin snowflake ids increase over time; pinned old posts sit first.
            if not best_id or int(vid) > int(best_id):
                best_id = vid
                best_url = f"https://www.douyin.com/video/{vid}"
        if not best_id:
            return None
        return LatestVideoEntry(
            video_id=best_id,
            url=best_url,
            title=best_id,
            platform="douyin",
        )

    try:
        return asyncio.run(_run())
    except Exception:
        return None


def _fetch_bilibili_latest_via_api(mid: str) -> LatestVideoEntry | None:
    """Best-effort public API; may be rate-limited."""
    import urllib.error
    import urllib.request

    api = f"https://api.bilibili.com/x/space/arc/search?mid={mid}&ps=5&pn=1&order=pubdate"
    req = urllib.request.Request(
        api,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": f"https://space.bilibili.com/{mid}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if payload.get("code") != 0:
        return None
    vlist = (((payload.get("data") or {}).get("list") or {}).get("vlist") or [])
    if not vlist:
        return None
    item = vlist[0]
    bvid = str(item.get("bvid") or "").strip()
    title = str(item.get("title") or bvid).strip()
    if not bvid:
        return None
    return LatestVideoEntry(
        video_id=bvid,
        url=f"https://www.bilibili.com/video/{bvid}",
        title=title,
        platform="bilibili",
    )


def fetch_latest_video_from_profile(raw_url: str) -> LatestVideoEntry:
    """Resolve creator homepage / profile URL to the newest video entry."""
    url = extract_url(raw_url)
    platform = detect_platform(url)

    if platform == "douyin":
        resolved = _follow_redirects(url)
        sec_uid = _extract_douyin_sec_uid(resolved) or _extract_douyin_sec_uid(url)
        if sec_uid:
            hit = _fetch_douyin_latest_via_browser(sec_uid)
            if hit:
                return hit
        raise RuntimeError(
            "无法从抖音主页解析最新作品；请确认链接是博主主页，并已绑定有效的抖音 Cookie"
        )

    if platform == "bilibili":
        mid = _extract_bilibili_mid(url)
        if mid:
            api_hit = _fetch_bilibili_latest_via_api(mid)
            if api_hit:
                return api_hit

    work_dir = _downloads_root() / f"profile_{uuid.uuid4().hex[:12]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    profile_url = url
    if platform == "bilibili" and "/video" not in urlparse(url).path:
        # Prefer the uploads tab when possible.
        mid = _extract_bilibili_mid(url)
        if mid:
            profile_url = f"https://space.bilibili.com/{mid}/video"

    cmd = [
        *_yt_dlp_cmd(),
        "--flat-playlist",
        "--playlist-end",
        "8",
        "--print",
        "%(id)s\t%(webpage_url)s\t%(title)s",
        *_prepare_cookies_arg(platform, work_dir),
        profile_url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("拉取博主作品列表超时") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        hint = ""
        if platform == "douyin":
            hint = "；请确认主页链接有效，并已绑定抖音账号 Cookie"
        elif platform == "bilibili":
            hint = "；B站主页接口可能风控，请稍后重试或换链接"
        raise RuntimeError((err[-1500:] if err else "拉取博主作品失败") + hint)

    entries: list[LatestVideoEntry] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        video_id = parts[0].strip()
        video_url = parts[1].strip()
        title = parts[2].strip() if len(parts) > 2 else video_id
        if not video_id or not video_url.startswith("http"):
            continue
        entries.append(
            LatestVideoEntry(video_id=video_id, url=video_url, title=title or video_id, platform=platform)
        )

    if not entries:
        result = download_share_url(url, output_dir=work_dir / "fallback")
        video_id = result.file_path.stem
        return LatestVideoEntry(
            video_id=video_id,
            url=result.source_url,
            title=result.title,
            platform=platform,
        )

    return entries[0]


def _downloads_root() -> Path:
    root = Path(BASE_DIR) / "tmp" / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _find_platform_cookie_json(platform: str) -> Path | None:
    cookies_dir = Path(BASE_DIR) / "cookies"
    if not cookies_dir.exists():
        return None
    matches = sorted(cookies_dir.glob(f"{platform}_*.json"))
    return matches[0] if matches else None


def storage_state_to_netscape(src: Path, dest: Path) -> int:
    """Convert Playwright storage_state JSON to Netscape cookies.txt for yt-dlp."""
    data = json.loads(src.read_text(encoding="utf-8"))
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated from Playwright storage_state for yt-dlp",
        "",
    ]
    count = 0
    for cookie in data.get("cookies") or []:
        domain = cookie.get("domain") or ""
        name = cookie.get("name") or ""
        if not domain or not name:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = cookie.get("path") or "/"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires")
        if expires is None or expires < 0:
            expires_i = 0
        else:
            expires_i = int(expires)
        value = str(cookie.get("value") or "")
        lines.append(
            "\t".join([domain, include_sub, path, secure, str(expires_i), name, value])
        )
        count += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count


def _prepare_cookies_arg(platform: str, target_dir: Path) -> list[str]:
    """Douyin/Kuaishou usually need cookies; reuse bound account storage_state."""
    if platform not in ("douyin", "kuaishou"):
        return []
    cookie_json = _find_platform_cookie_json(platform)
    if not cookie_json:
        return []
    netscape = target_dir / f"{platform}_cookies.txt"
    count = storage_state_to_netscape(cookie_json, netscape)
    if count <= 0:
        return []
    return ["--cookies", str(netscape)]


def download_share_url(raw_url: str, output_dir: Path | None = None) -> DownloadResult:
    url = extract_url(raw_url)
    platform = detect_platform(url)
    target_dir = Path(output_dir) if output_dir else _downloads_root() / uuid.uuid4().hex
    target_dir.mkdir(parents=True, exist_ok=True)
    download_id = target_dir.name

    outtmpl = str(target_dir / "%(title).80B [%(id)s].%(ext)s")
    cmd = [
        *_yt_dlp_cmd(),
        "--no-playlist",
        "--newline",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        outtmpl,
        "--print",
        "after_move:filepath",
        "--print",
        "after_move:title",
        *_prepare_cookies_arg(platform, target_dir),
        url,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("下载超时（超过 10 分钟）") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        hint = ""
        if platform == "douyin":
            if not _find_platform_cookie_json("douyin"):
                hint = "；请先在 Web 里扫码绑定一个抖音账号，下载会自动带上 Cookie"
            else:
                hint = "；已尝试使用绑定账号 Cookie 仍失败，请重新扫码登录抖音后再试"
        raise RuntimeError((err[-2000:] if err else "下载失败") + hint)

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    file_path: Path | None = None
    title = ""
    if len(lines) >= 2:
        file_path = Path(lines[-2])
        title = lines[-1]
    elif lines:
        maybe = Path(lines[-1])
        if maybe.exists():
            file_path = maybe

    if file_path is None or not file_path.exists():
        candidates = sorted(
            [p for p in target_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".flv"}],
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError("下载完成但未找到视频文件")
        file_path = candidates[0]
        title = file_path.stem

    return DownloadResult(
        download_id=download_id,
        platform=platform,
        source_url=url,
        file_path=file_path,
        title=title or file_path.stem,
        filename=file_path.name,
    )
