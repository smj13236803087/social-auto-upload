"""Download Douyin / Kuaishou / Bilibili / YouTube videos from share/profile URLs via yt-dlp."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from conf import BASE_DIR

SUPPORTED_PLATFORMS = ("douyin", "kuaishou", "bilibili", "youtube")

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_DOUYIN_SEC_UID_RE = re.compile(r"(?:/user/|/share/user/)(MS4wLjABAAAA[\w-]+)")
_DOUYIN_VIDEO_ID_RE = re.compile(r"/video/(\d+)")
_YT_EXTRACTOR_ARGS = "youtube:player_client=default,web_embedded"


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


def _youtube_extra_args() -> list[str]:
    """Proxy + extractor args for YouTube (needed when youtube.com is blocked)."""
    args = [
        "--extractor-args",
        _YT_EXTRACTOR_ARGS,
        "--socket-timeout",
        "15",
        "--retries",
        "2",
    ]
    proxy = ""
    try:
        from conf import YT_PROXY

        proxy = str(YT_PROXY or "").strip()
    except Exception:
        proxy = ""
    if not proxy:
        proxy = (os.environ.get("AUTOSELF_YT_PROXY") or os.environ.get("YT_PROXY") or "").strip()
    if proxy:
        args.extend(["--proxy", proxy])
    return args


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
    if "youtube.com" in host or "youtu.be" in host or "youtube-nocookie.com" in host:
        return "youtube"
    raise ValueError("仅支持抖音 / 快手 / B站 / YouTube 视频或主页链接")


def _normalize_youtube_list_url(url: str) -> str:
    """Prefer channel Shorts tab for @handle / channel home URLs."""
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    lower = path.lower()
    if any(
        part in lower
        for part in ("/shorts", "/videos", "/streams", "/playlist", "/watch", "/live")
    ):
        return url
    if (
        path.startswith("/@")
        or "/channel/" in lower
        or path.startswith("/c/")
        or path.startswith("/user/")
    ):
        return f"{parsed.scheme}://{parsed.netloc}{path}/shorts"
    return url


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
    """Open douyin.com/user/<sec_uid> with bound Cookie and pick newest *own* video.

    Prefer aweme/post API payloads (create_time ordered). DOM scraping of all
    a[href*=/video/] is wrong: the page also embeds recommended/other creators'
    videos whose snowflake ids can be newer than the blogger's latest post.
    """
    import asyncio

    cookie_json = _find_platform_cookie_json("douyin")
    if not cookie_json:
        return None

    async def _run() -> LatestVideoEntry | None:
        from patchright.async_api import async_playwright

        profile_url = f"https://www.douyin.com/user/{sec_uid}"
        posts: list[tuple[int, str, str]] = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                storage_state=str(cookie_json),
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            async def on_response(response) -> None:
                try:
                    url = str(response.url or "")
                    # Only the creator's own「作品」list — not related/recommend feeds.
                    if "aweme/v1/web/aweme/post" not in url and "/aweme/post/" not in url:
                        return
                    if "sec_user_id=" in url and sec_uid not in url:
                        return
                    if response.status != 200:
                        return
                    payload = await response.json()
                except Exception:
                    return
                for aweme in payload.get("aweme_list") or []:
                    if not isinstance(aweme, dict):
                        continue
                    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
                    author_sec = str(author.get("sec_uid") or author.get("secUid") or "").strip()
                    if author_sec and author_sec != sec_uid:
                        continue
                    vid = str(aweme.get("aweme_id") or aweme.get("awemeId") or "").strip()
                    if not vid.isdigit():
                        continue
                    create_time = int(aweme.get("create_time") or aweme.get("createTime") or 0)
                    title = str(aweme.get("desc") or aweme.get("preview_title") or vid).strip()
                    posts.append((create_time, vid, title or vid))

            page.on("response", on_response)
            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3500)
                # Close trust/login masks that block the「作品」tab.
                try:
                    await page.evaluate(
                        """() => {
                          document.querySelectorAll(
                            '.trust-login-dialog-mask, #trust-logout-dialog, [class*="login-mask"]'
                          ).forEach(el => el.remove());
                        }"""
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(2000)
                # Ensure「作品」tab (not 喜欢/收藏) so post API / grid match uploads.
                try:
                    for label in ("作品", "作品 0", "作品0"):
                        loc = page.get_by_text(label, exact=True)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=2000, force=True)
                            await page.wait_for_timeout(2500)
                            break
                except Exception:
                    pass
                # Extra settle for late post API
                await page.wait_for_timeout(2000)
            finally:
                await browser.close()

        if posts:
            # Newest by publish time; snowflake id as tie-break.
            posts.sort(key=lambda row: (row[0], int(row[1])), reverse=True)
            _ct, vid, title = posts[0]
            return LatestVideoEntry(
                video_id=vid,
                url=f"https://www.douyin.com/video/{vid}",
                title=title or vid,
                platform="douyin",
            )

        # Do NOT fall back to scraping all a[href*=/video/] — those include recommends.
        return None

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
    elif platform == "youtube":
        profile_url = _normalize_youtube_list_url(url)

    playlist_end = "30" if platform == "youtube" else "12"
    list_timeout = 55 if platform == "youtube" else 180
    cmd = [
        *_yt_dlp_cmd(),
        "--flat-playlist",
        "--playlist-end",
        playlist_end,
        "--print",
        "%(id)s\t%(webpage_url)s\t%(title)s\t%(timestamp)s\t%(upload_date)s",
        *_prepare_cookies_arg(platform, work_dir),
        *(_youtube_extra_args() if platform == "youtube" else []),
        profile_url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=list_timeout,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        if platform == "youtube":
            raise RuntimeError(
                "拉取 YouTube 作品超时：服务器直连 YouTube 不通。"
                "请在服务器配置可用代理后设置 conf.YT_PROXY 或环境变量 AUTOSELF_YT_PROXY"
                "（例如 http://127.0.0.1:7890），再重启服务"
            ) from exc
        raise RuntimeError("拉取博主作品列表超时") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        hint = ""
        if platform == "douyin":
            hint = "；请确认主页链接有效，并已绑定抖音账号 Cookie"
        elif platform == "bilibili":
            hint = "；B站主页接口可能风控，请稍后重试或换链接"
        elif platform == "youtube":
            proxy_on = bool(
                (os.environ.get("AUTOSELF_YT_PROXY") or os.environ.get("YT_PROXY") or "").strip()
            )
            try:
                from conf import YT_PROXY

                proxy_on = proxy_on or bool(str(YT_PROXY or "").strip())
            except Exception:
                pass
            if not proxy_on:
                hint = (
                    "；当前未配置 YouTube 代理。服务器直连 YouTube 不可用，"
                    "请设置 conf.YT_PROXY 或 AUTOSELF_YT_PROXY 后重启"
                )
            else:
                hint = "；已配置代理但仍失败，请检查代理是否可用、协议是否为 http/socks5"
        raise RuntimeError((err[-1500:] if err else "拉取博主作品失败") + hint)
    entries: list[tuple[int, LatestVideoEntry]] = []
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
        ts_raw = parts[3].strip() if len(parts) > 3 else ""
        date_raw = parts[4].strip() if len(parts) > 4 else ""
        if not video_id or video_id in {"NA", "None"}:
            continue
        if not video_url.startswith("http"):
            if platform == "youtube" and video_id:
                video_url = f"https://www.youtube.com/shorts/{video_id}"
            else:
                continue
        sort_key = 0
        if ts_raw.isdigit():
            sort_key = int(ts_raw)
        elif len(date_raw) == 8 and date_raw.isdigit():
            # upload_date YYYYMMDD → rough epoch-ish for ordering
            sort_key = int(date_raw)
        entries.append(
            (
                sort_key,
                LatestVideoEntry(
                    video_id=video_id,
                    url=video_url,
                    title=title or video_id,
                    platform=platform,
                ),
            )
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

    # Prefer highest timestamp/upload_date; if all zero, keep playlist order (first).
    if any(k > 0 for k, _ in entries):
        entries.sort(key=lambda row: row[0], reverse=True)
    return entries[0][1]


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


def _extract_douyin_video_id(url: str) -> str | None:
    m = _DOUYIN_VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def _pick_url_list(node: dict | None) -> str | None:
    if not isinstance(node, dict):
        return None
    for item in node.get("url_list") or node.get("download_url_list") or []:
        text = str(item or "").strip()
        if text.startswith("http"):
            return text
    return None


def _http_download(url: str, dest: Path, *, timeout: int = 120) -> Path:
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
    if not dest.exists() or dest.stat().st_size < 64:
        raise RuntimeError(f"下载失败或文件过小: {dest.name}")
    return dest


def _fetch_douyin_aweme_detail(video_id: str) -> dict | None:
    """Load aweme detail via bound Douyin cookie + headless browser (signed web API)."""
    import asyncio

    cookie_json = _find_platform_cookie_json("douyin")
    if not cookie_json or not video_id:
        return None

    async def _run() -> dict | None:
        from patchright.async_api import async_playwright

        detail: dict | None = None

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                storage_state=str(cookie_json),
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            async def on_response(response) -> None:
                nonlocal detail
                try:
                    url = str(response.url or "")
                    if "aweme/v1/web/aweme/detail" not in url:
                        return
                    if response.status != 200:
                        return
                    payload = await response.json()
                    node = payload.get("aweme_detail")
                    if isinstance(node, dict) and str(node.get("aweme_id") or "") == str(video_id):
                        detail = node
                except Exception:
                    return

            page.on("response", on_response)
            try:
                await page.goto(
                    f"https://www.douyin.com/video/{video_id}",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                for _ in range(20):
                    if detail:
                        break
                    await page.wait_for_timeout(500)
            finally:
                await browser.close()
        return detail

    try:
        return asyncio.run(_run())
    except Exception:
        return None


def _ffmpeg_bin() -> str:
    from shutil import which

    path = which("ffmpeg")
    if not path:
        raise RuntimeError("服务器未安装 ffmpeg，无法将抖音图文合成视频")
    return path


def _ffprobe_duration(path: Path) -> float | None:
    from shutil import which

    ffprobe = which("ffprobe")
    if not ffprobe or not path.exists():
        return None
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return None


def _compose_note_mp4(*, image_paths: list[Path], audio_path: Path | None, dest: Path) -> Path:
    """Image note → mp4；有配乐时视频时长跟音乐对齐（与抖音图文一致）。"""
    if not image_paths:
        raise RuntimeError("图文作品没有可下载的图片")
    ffmpeg = _ffmpeg_bin()
    has_audio = bool(audio_path and audio_path.exists())
    audio_dur = _ffprobe_duration(audio_path) if has_audio else None

    # 单图 + 音乐：循环静帧直到音乐播完（最接近原作）
    if has_audio and len(image_paths) == 1 and audio_dur and audio_dur > 0.5:
        cmd = [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_paths[0]),
            "-i",
            str(audio_path),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-r",
            "25",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not dest.exists():
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"图文合成视频失败：{(err[-800:] if err else 'ffmpeg error')}")
        return dest

    # 多图：按音乐总时长均分每张停留；无音乐则每张 4 秒
    n = len(image_paths)
    if has_audio and audio_dur and audio_dur > 0.5:
        per_image = max(audio_dur / n, 0.5)
    else:
        per_image = 4.0

    list_file = dest.with_suffix(".ffconcat")
    lines = ["ffconcat version 1.0"]
    for img in image_paths:
        lines.append(f"file '{img.resolve()}'")
        lines.append(f"duration {per_image:.3f}")
    lines.append(f"file '{image_paths[-1].resolve()}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
    ]
    if has_audio:
        # 视频已按音乐时长铺满，不再用 -shortest（否则会被短图轨裁掉）
        cmd.extend(["-i", str(audio_path), "-c:a", "aac"])
    cmd.extend(
        [
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    if has_audio and audio_dur and audio_dur > 0.5:
        cmd.extend(["-t", f"{audio_dur:.3f}"])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not dest.exists():
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"图文合成视频失败：{(err[-800:] if err else 'ffmpeg error')}")
    return dest


def _download_douyin_via_browser(url: str, target_dir: Path) -> DownloadResult:
    """Fallback when yt-dlp cannot fetch Douyin (signed API / 图文笔记)."""
    video_id = _extract_douyin_video_id(url)
    if not video_id:
        resolved = _follow_redirects(url)
        video_id = _extract_douyin_video_id(resolved)
    if not video_id:
        raise RuntimeError("无法识别抖音视频 ID")

    detail = _fetch_douyin_aweme_detail(video_id)
    if not detail:
        raise RuntimeError("无法获取抖音作品详情（Cookie 可能过期，请重新扫码登录抖音）")

    title = str(detail.get("desc") or detail.get("preview_title") or video_id).strip() or video_id
    safe_title = re.sub(r"[\\/:*?\"<>|\s]+", "_", title)[:60] or video_id
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}

    # Real video: prefer bit_rate / play_addr mp4
    mp4_url = None
    for br in video.get("bit_rate") or []:
        if not isinstance(br, dict):
            continue
        mp4_url = _pick_url_list(br.get("play_addr"))
        if mp4_url and ".mp3" not in mp4_url:
            break
    if not mp4_url:
        cand = _pick_url_list(video.get("play_addr"))
        if cand and ".mp3" not in cand and "ies-music" not in cand:
            mp4_url = cand

    if mp4_url:
        dest = target_dir / f"{safe_title} [{video_id}].mp4"
        _http_download(mp4_url, dest)
        return DownloadResult(
            download_id=target_dir.name,
            platform="douyin",
            source_url=url,
            file_path=dest,
            title=title,
            filename=dest.name,
        )

    # 图文笔记（aweme_type=68 等）：图片 + 音乐合成 mp4，才能发到 B 站
    images = detail.get("images") if isinstance(detail.get("images"), list) else []
    image_paths: list[Path] = []
    for idx, img in enumerate(images):
        if not isinstance(img, dict):
            continue
        img_url = _pick_url_list(img) or _pick_url_list({"url_list": img.get("download_url_list") or []})
        if not img_url:
            continue
        ext = ".jpg"
        if ".webp" in img_url:
            ext = ".webp"
        elif ".png" in img_url:
            ext = ".png"
        path = target_dir / f"img_{idx:02d}{ext}"
        try:
            _http_download(img_url, path)
            image_paths.append(path)
        except Exception:
            continue

    audio_path = None
    music = detail.get("music") if isinstance(detail.get("music"), dict) else {}
    audio_url = _pick_url_list(music.get("play_url")) or _pick_url_list(video.get("play_addr"))
    if audio_url:
        audio_path = target_dir / f"audio_{video_id}.mp3"
        try:
            _http_download(audio_url, audio_path)
        except Exception:
            audio_path = None

    if not image_paths:
        raise RuntimeError("该抖音作品是图文/非视频，且未能下载到图片，无法发布到 B 站")

    dest = target_dir / f"{safe_title} [{video_id}].mp4"
    _compose_note_mp4(image_paths=image_paths, audio_path=audio_path, dest=dest)
    return DownloadResult(
        download_id=target_dir.name,
        platform="douyin",
        source_url=url,
        file_path=dest,
        title=title,
        filename=dest.name,
    )


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
        *(_youtube_extra_args() if platform == "youtube" else []),
        url,
    ]

    yt_err = ""
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

    if proc.returncode == 0:
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
            if candidates:
                file_path = candidates[0]
                title = file_path.stem

        if file_path is not None and file_path.exists():
            return DownloadResult(
                download_id=download_id,
                platform=platform,
                source_url=url,
                file_path=file_path,
                title=title or file_path.stem,
                filename=file_path.name,
            )

    yt_err = (proc.stderr or proc.stdout or "").strip()

    # Douyin: yt-dlp often fails on signed APIs / 图文笔记；用浏览器详情接口兜底。
    if platform == "douyin":
        try:
            return _download_douyin_via_browser(url, target_dir)
        except Exception as browser_exc:
            if not _find_platform_cookie_json("douyin"):
                hint = "；请先在 Web 里扫码绑定一个抖音账号"
            else:
                hint = f"；浏览器兜底也失败：{browser_exc}"
            raise RuntimeError((yt_err[-1200:] if yt_err else "下载失败") + hint) from browser_exc

    hint = ""
    raise RuntimeError((yt_err[-2000:] if yt_err else "下载失败") + hint)
