#!/usr/bin/env python3
"""Daily sync: YouTube Shorts (newest first) -> Kuaishou + Bilibili.

Rules:
- Shorts only (default channel @kochiasmr/shorts)
- Schedule 09:00 / 16:00 / 21:00 Beijing time, 1 short per run (= 3/day)
- Prefer H.264 1080p download (Douyin-friendly codecs also fine for KS/Bili)
- After download, verify H.264 clarity; if soft, pin best format and re-download once
- If still soft, skip to next short (max 5 tries / run), then stop the job
- Each run must publish a brand-new short
- If a short already has download/platform history, skip to the next unused short
- Chinese copy from food_process_copy_library.json in order
- Delete local inbox files after Kuaishou + Bilibili succeed
- Douyin is a separate job (sync_yt_douyin.py)
- Xiaohongshu iCloud staging is a separate job (sync_yt_xhs_stage.py)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANNEL = "https://www.youtube.com/@kochiasmr/shorts"
DEFAULT_ACCOUNT = "boss rabbit"
DEFAULT_STATE = ROOT / "data" / "yt_food_sync_state.json"
LEGACY_STATE = ROOT / "data" / "yt_xhs_sync_state.json"
DEFAULT_INBOX = ROOT / "videos" / "yt_food_inbox"
DEFAULT_COPY_LIBRARY = ROOT / "data" / "food_process_copy_library.json"
DEFAULT_COOKIES_FROM_BROWSER = "edge"
DEFAULT_KS_TAGS = "沉浸式"
DEFAULT_DY_TAGS = "美食,制作过程,治愈"
DEFAULT_XHS_TAGS = "美食,制作过程,治愈,跟做"
DEFAULT_BILI_TID = 249
DEFAULT_BILI_TAGS = "美食,制作过程,治愈"
# Auto-upload targets for this job.
FOOD_PLATFORMS = ("kuaishou", "bilibili")
# Any of these means the short is already consumed (shared with douyin/xhs jobs).
CLAIM_FIELDS = (
    "downloaded",
    "xhs_staged",
    "kuaishou",
    "bilibili",
    "douyin",
    "quality_rejected",
)
PLATFORMS = ("kuaishou", "bilibili", "douyin")  # mark-uploaded allowlist
# Per scheduled run: try at most N shorts for clarity; then stop the job.
DEFAULT_MAX_QUALITY_TRIES = 5
BEIJING = ZoneInfo("Asia/Shanghai")


class QualityRejected(RuntimeError):
    """Downloaded file still below required clarity after pin-retry."""


# Portrait Shorts: 1080p means width=1080 (height~1920). Filtering height=1080 wrongly picks 480p.
DEFAULT_YT_FORMAT = (
    "bv*[vcodec^=avc1][width=1080]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1][height=1080]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1][width>=720]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1]+ba[ext=m4a]/"
    "b[ext=mp4]/"
    "bv*+ba/b"
)
DEFAULT_XHS_STAGE_DIR = (
    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/sau-xhs-待发"
)


def _beijing_now() -> datetime:
    return datetime.now(BEIJING)


def _now_iso() -> str:
    return _beijing_now().isoformat(timespec="seconds")


def _empty_entry() -> dict:
    return {
        "source_title": "",
        "url": "",
        "kuaishou": None,
        "bilibili": None,
        "douyin": None,
        "copy": None,
        "quality_rejected": None,
        "quality_reject_reason": None,
    }


def _normalize_entry(entry: dict) -> dict:
    entry.setdefault("source_title", "")
    entry.setdefault("url", "")
    entry.setdefault("kuaishou", None)
    entry.setdefault("bilibili", None)
    entry.setdefault("douyin", None)
    entry.setdefault("copy", None)
    entry.setdefault("quality_rejected", None)
    entry.setdefault("quality_reject_reason", None)
    return entry


def _load_state(path: Path) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    elif LEGACY_STATE.exists():
        data = json.loads(LEGACY_STATE.read_text(encoding="utf-8"))
        print(f"migrated state from {LEGACY_STATE.name}", flush=True)
    else:
        data = {"channel": "", "copy_index": 0, "items": {}}
    data.setdefault("copy_index", 0)
    data.setdefault("items", {})
    for entry in data["items"].values():
        if isinstance(entry, dict):
            _normalize_entry(entry)
    return data


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_copy_library(path: Path) -> list[dict]:
    if not path.exists():
        raise RuntimeError(f"文案库不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    copies = data.get("copies") or []
    if not copies:
        raise RuntimeError(f"文案库为空: {path}")
    return copies


def allocate_copy(state: dict, copies: list[dict]) -> dict:
    idx = int(state.get("copy_index", 0))
    if idx >= len(copies):
        print(f"文案库已用完（共 {len(copies)} 条），从头循环", flush=True)
        idx = 0
    item = copies[idx]
    state["copy_index"] = idx + 1
    return {
        "id": item.get("id", idx + 1),
        "title": item["title"],
        "desc": item.get("desc") or item["title"],
        "index": idx,
    }


def _venv_bin(name: str) -> str:
    candidate = ROOT / ".venv" / "bin" / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"找不到命令: {name}")
    return found


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    env = os.environ.copy()
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    return subprocess.run(cmd, cwd=str(ROOT), env=env, check=check, text=True)


def _yt_dlp_cookie_args(cookies_from_browser: str | None) -> list[str]:
    if not cookies_from_browser:
        return []
    return ["--cookies-from-browser", cookies_from_browser]


def list_shorts(channel: str, lookback: int, cookies_from_browser: str | None = None) -> list[dict]:
    yt_dlp = shutil.which("yt-dlp") or "yt-dlp"
    cmd = [
        yt_dlp,
        "--no-update",
        *_yt_dlp_cookie_args(cookies_from_browser),
        "--flat-playlist",
        "-I",
        f"1:{lookback}",
        "--print",
        "%(id)s\t%(title)s\t%(webpage_url)s",
        channel,
    ]
    print("+", " ".join(cmd), flush=True)
    env = os.environ.copy()
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, check=True, text=True, capture_output=True)
    items: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        video_id, title, url = parts[0], parts[1], parts[2]
        if not video_id or video_id == "NA":
            continue
        items.append({"id": video_id, "title": title, "url": url})
    return items


def is_done(entry: dict | None) -> bool:
    """Kuaishou + Bilibili both done for this job."""
    if not entry:
        return False
    return all(bool(entry.get(p)) for p in FOOD_PLATFORMS)


def is_claimed(entry: dict | None) -> bool:
    """Already consumed by food/douyin/xhs pipeline — never reuse."""
    if not entry:
        return False
    return any(bool(entry.get(field)) for field in CLAIM_FIELDS)


def pick_next(items: list[dict], state: dict) -> dict | None:
    records = state.setdefault("items", {})
    for item in items:
        if not is_claimed(records.get(item["id"])):
            return item
    return None


def _probe_video(path: Path) -> dict:
    """Return width/height/codec from local mp4 via ffprobe."""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name",
            "-of",
            "json",
            str(path),
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=True,
    )
    streams = (json.loads(proc.stdout) or {}).get("streams") or []
    if not streams:
        raise RuntimeError(f"ffprobe 未读到视频轨: {path}")
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    codec = str(stream.get("codec_name") or "")
    if width <= 0 or height <= 0:
        raise RuntimeError(f"ffprobe 分辨率异常: {path} -> {width}x{height}")
    return {"width": width, "height": height, "codec": codec}


def _is_avc_format(fmt: dict) -> bool:
    vcodec = str(fmt.get("vcodec") or "").lower()
    return vcodec.startswith("avc1") or vcodec.startswith("avc") or vcodec == "h264"


def _best_avc_dims_from_info(info: dict) -> tuple[int, int, str]:
    """Best H.264 progressive dims from yt-dlp info.json formats list."""
    best_w = best_h = 0
    best_id = ""
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        if not _is_avc_format(fmt):
            continue
        width = int(fmt.get("width") or 0)
        height = int(fmt.get("height") or 0)
        if width <= 0 or height <= 0:
            continue
        # Skip audio-only / tiny stubs.
        if width * height < 160 * 160:
            continue
        if width * height > best_w * best_h:
            best_w, best_h = width, height
            best_id = str(fmt.get("format_id") or "")
    return best_w, best_h, best_id


def _load_info_json(inbox: Path, video_id: str) -> tuple[dict | None, Path | None]:
    infos = sorted(inbox.glob(f"*{video_id}*.info.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for info_path in infos:
        try:
            data = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("_type") == "playlist":
            continue
        if data.get("id") == video_id:
            return data, info_path
    return None, None


def _pinned_best_avc_selector(info: dict) -> str | None:
    """Force exact best H.264 video id + best m4a audio."""
    _best_w, _best_h, best_id = _best_avc_dims_from_info(info)
    if not best_id:
        return None
    return f"{best_id}+bestaudio[ext=m4a]/{best_id}+bestaudio/{best_id}"


def evaluate_highest_quality(
    video_path: Path, info: dict | None
) -> tuple[bool, dict, str]:
    """Return (ok, probe, detail). Soft-fail when below best available H.264."""
    probe = _probe_video(video_path)
    got_w, got_h = probe["width"], probe["height"]
    got_pixels = got_w * got_h

    best_w = best_h = 0
    best_id = ""
    if info:
        best_w, best_h, best_id = _best_avc_dims_from_info(info)

    if max(got_w, got_h) < 720:
        return (
            False,
            probe,
            f"本地 {got_w}x{got_h} {probe['codec']} 低于 720p 底线",
        )

    if best_w > 0 and best_h > 0:
        best_pixels = best_w * best_h
        detail = (
            f"本地 {got_w}x{got_h} {probe['codec']} vs 最佳 H.264 "
            f"{best_w}x{best_h}"
            + (f" #{best_id}" if best_id else "")
        )
        if got_pixels < int(best_pixels * 0.9):
            return False, probe, f"未达最高档: {detail}"
        return True, probe, detail

    if max(got_w, got_h) < 1080:
        return (
            False,
            probe,
            f"无格式列表且本地仅 {got_w}x{got_h}，未达 1080 档",
        )
    return True, probe, f"本地 {got_w}x{got_h} {probe['codec']} (无格式列表，已达 1080 档)"


def assert_highest_quality(video_path: Path, info: dict | None) -> dict:
    ok, probe, detail = evaluate_highest_quality(video_path, info)
    if not ok:
        raise QualityRejected(f"清晰度不合格: {detail}")
    print(f"quality check ok: {detail}", flush=True)
    return probe


def mark_quality_rejected(
    state: dict,
    entry: dict,
    *,
    video_id: str,
    inbox: Path,
    reason: str,
    state_path: Path | None = None,
) -> None:
    """Consume this short so later runs skip it; clean local files."""
    entry["quality_rejected"] = _now_iso()
    entry["quality_reject_reason"] = (reason or "")[:400]
    cleanup_local(inbox, video_id)
    if state_path is not None:
        _save_state(state_path, state)
    print(
        f"quality rejected, skip video: {video_id} | {entry['quality_reject_reason']}",
        flush=True,
    )


def _download_with_format(
    url: str,
    video_id: str,
    inbox: Path,
    format_selector: str,
    cookies_from_browser: str | None = None,
) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    for p in inbox.glob(f"*{video_id}*"):
        p.unlink(missing_ok=True)

    yt_dlp = shutil.which("yt-dlp") or "yt-dlp"
    out_tmpl = str(inbox / f"food_%(upload_date)s_%(id)s_%(title).80B.%(ext)s")
    _run(
        [
            yt_dlp,
            "--no-update",
            *_yt_dlp_cookie_args(cookies_from_browser),
            "-f",
            format_selector,
            "--merge-output-format",
            "mp4",
            "-o",
            out_tmpl,
            "--write-info-json",
            url,
        ]
    )
    videos = sorted(inbox.glob(f"*{video_id}*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not videos:
        raise RuntimeError(f"下载完成但未找到 mp4: {video_id}")
    return videos[0]


def download_highest(
    url: str,
    video_id: str,
    inbox: Path,
    cookies_from_browser: str | None = None,
    format_selector: str = DEFAULT_YT_FORMAT,
) -> tuple[Path, str]:
    """Download best practical H.264; if soft, pin best format and retry once."""
    video_path = _download_with_format(
        url, video_id, inbox, format_selector, cookies_from_browser=cookies_from_browser
    )
    info, _ = _load_info_json(inbox, video_id)
    title = (info or {}).get("title") or video_id

    ok, probe, detail = evaluate_highest_quality(video_path, info)
    if not ok:
        pinned = _pinned_best_avc_selector(info or {})
        if not pinned:
            raise QualityRejected(f"清晰度不合格且无法锁定更高清格式: {detail}")
        print(
            f"quality low ({detail}); re-download pinned format: {pinned}",
            flush=True,
        )
        video_path = _download_with_format(
            url, video_id, inbox, pinned, cookies_from_browser=cookies_from_browser
        )
        info, _ = _load_info_json(inbox, video_id)
        if info and info.get("title"):
            title = info["title"]
        ok, probe, detail = evaluate_highest_quality(video_path, info)
        if not ok:
            raise QualityRejected(f"重下后仍不达标: {detail}")

    print(f"quality check ok: {detail}", flush=True)
    print(
        f"ready to publish: {probe['width']}x{probe['height']} {probe['codec']}",
        flush=True,
    )
    return video_path, title


def check_platform(sau: str, platform: str, account: str) -> bool:
    proc = subprocess.run(
        [sau, platform, "check", "--account", account],
        cwd=str(ROOT),
        env={k: v for k, v in os.environ.items() if k != "PLAYWRIGHT_BROWSERS_PATH"},
        text=True,
        capture_output=True,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print(out, flush=True)
    return proc.returncode == 0 and "valid" in out.splitlines()[-1]


def upload_kuaishou(sau: str, account: str, video: Path, title: str, desc: str, tags: str) -> None:
    cmd = [
        sau,
        "kuaishou",
        "upload-video",
        "--account",
        account,
        "--file",
        str(video),
        "--title",
        title[:100],
        "--desc",
        desc[:500],
    ]
    if tags.strip():
        cmd.extend(["--tags", tags.strip()])
    _run(cmd)


def upload_bilibili(
    sau: str, account: str, video: Path, title: str, desc: str, tid: int, tags: str
) -> None:
    _run(
        [
            sau,
            "bilibili",
            "upload-video",
            "--account",
            account,
            "--file",
            str(video),
            "--title",
            title[:80],
            "--desc",
            desc[:250],
            "--tid",
            str(tid),
            "--tags",
            tags,
        ]
    )


def upload_douyin(sau: str, account: str, video: Path, title: str, desc: str, tags: str) -> None:
    cmd = [
        sau,
        "douyin",
        "upload-video",
        "--account",
        account,
        "--file",
        str(video),
        "--title",
        title[:20],
        "--desc",
        desc[:1000],
    ]
    if tags.strip():
        cmd.extend(["--tags", tags.strip()])
    _run(cmd)


def cleanup_local(inbox: Path, video_id: str) -> None:
    for p in inbox.glob(f"*{video_id}*"):
        p.unlink(missing_ok=True)
        print(f"deleted {p}", flush=True)


def _safe_name(text: str, max_len: int = 40) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    cleaned = "".join("_" if ch in bad else ch for ch in text).strip().strip(".")
    cleaned = cleaned or "untitled"
    return cleaned[:max_len]


def stage_for_xhs_manual(
    video_path: Path,
    *,
    video_id: str,
    title: str,
    desc: str,
    tags: str,
    stage_dir: Path,
) -> Path:
    """Copy video + copy text into iCloud so phone can post Xiaohongshu manually."""
    stamp = _beijing_now().strftime("%Y%m%d_%H%M")
    folder = stage_dir / f"{stamp}_{_safe_name(title)}_{video_id}"
    folder.mkdir(parents=True, exist_ok=True)

    dest_video = folder / f"{_safe_name(title)}.mp4"
    shutil.copy2(video_path, dest_video)

    note = (
        f"标题：{title[:20]}\n"
        f"正文：{desc}\n"
        f"话题：{' '.join('#' + t.strip() for t in tags.split(',') if t.strip())}\n"
        f"\n"
        f"源视频ID：{video_id}\n"
        f"手机操作：打开「文件」App → iCloud 云盘 → sau-xhs-待发 → 本文件夹\n"
        f"发完后可删除本文件夹。\n"
    )
    (folder / "文案.txt").write_text(note, encoding="utf-8")
    print(f"xhs manual stage: {folder}", flush=True)
    return folder


def _folder_stage_date(name: str) -> date | None:
    """Parse YYYYMMDD from staged folder name like 20260814_0800_标题_id."""
    if len(name) < 8 or not name[:8].isdigit():
        return None
    try:
        return date(int(name[:4]), int(name[4:6]), int(name[6:8]))
    except ValueError:
        return None


def cleanup_previous_day_xhs_stages(stage_dir: Path, today: date) -> int:
    """Delete iCloud staged folders from before today (previous day's 2 videos, etc.)."""
    if not stage_dir.exists():
        return 0
    deleted = 0
    for path in sorted(stage_dir.iterdir()):
        if not path.is_dir():
            continue
        folder_day = _folder_stage_date(path.name)
        if folder_day is None:
            continue
        if folder_day < today:
            shutil.rmtree(path, ignore_errors=True)
            print(f"deleted old xhs stage: {path.name}", flush=True)
            deleted += 1
    if deleted:
        print(f"cleaned {deleted} previous-day xhs stage folder(s)", flush=True)
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync YouTube Shorts to Kuaishou + Bilibili")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--daily-limit", type=int, default=1)
    parser.add_argument("--lookback", type=int, default=50)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--copy-library", type=Path, default=DEFAULT_COPY_LIBRARY)
    parser.add_argument("--cookies-from-browser", default=DEFAULT_COOKIES_FROM_BROWSER)
    parser.add_argument("--ks-tags", default=DEFAULT_KS_TAGS, help="Kuaishou tags without #")
    parser.add_argument("--bili-tid", type=int, default=DEFAULT_BILI_TID)
    parser.add_argument("--bili-tags", default=DEFAULT_BILI_TAGS, help="Bilibili tags without #")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mark-uploaded",
        nargs=2,
        metavar=("VIDEO_ID", "PLATFORM"),
        help="Manually mark uploaded: platform=kuaishou|bilibili|douyin",
    )
    args = parser.parse_args()

    state = _load_state(args.state)
    state["channel"] = args.channel
    copies = _load_copy_library(args.copy_library)
    cookies_from_browser = (args.cookies_from_browser or "").strip() or None

    if args.mark_uploaded:
        video_id, platform = args.mark_uploaded
        platform = platform.lower()
        if platform not in PLATFORMS:
            print("PLATFORM 只能是 kuaishou / bilibili / douyin", file=sys.stderr)
            return 2
        entry = _normalize_entry(state.setdefault("items", {}).setdefault(video_id, _empty_entry()))
        entry[platform] = _now_iso()
        _save_state(args.state, state)
        print(f"marked {video_id} -> {platform}")
        return 0

    sau = _venv_bin("sau")
    print(f"checking cookies for account={args.account!r} (kuaishou/bilibili)", flush=True)
    ks_ok = check_platform(sau, "kuaishou", args.account)
    bili_ok = check_platform(sau, "bilibili", args.account)
    if not ks_ok or not bili_ok:
        print(
            "cookie 无效，请先在本机执行：\n"
            f'  sau kuaishou login --account "{args.account}"\n'
            f'  sau bilibili login --account "{args.account}"',
            file=sys.stderr,
        )
        return 3

    print(f"listing shorts from {args.channel} (lookback={args.lookback})", flush=True)
    items = list_shorts(args.channel, args.lookback, cookies_from_browser=cookies_from_browser)
    if not items:
        print("未获取到 Shorts 列表", file=sys.stderr)
        return 4

    processed = 0
    quality_tries = 0
    while processed < args.daily_limit:
        if quality_tries >= DEFAULT_MAX_QUALITY_TRIES:
            print(
                f"已试 {DEFAULT_MAX_QUALITY_TRIES} 个视频清晰度都不达标，本次发布任务停止",
                file=sys.stderr,
            )
            return 7

        nxt = pick_next(items, state)
        if not nxt:
            msg = "lookback 范围内没有未使用的新视频（快手+B站），本次未上传"
            print(msg, file=sys.stderr)
            if processed == 0:
                return 6
            break

        video_id = nxt["id"]
        quality_tries += 1
        entry = _normalize_entry(
            state.setdefault("items", {}).setdefault(
                video_id,
                {
                    "source_title": nxt["title"],
                    "url": nxt["url"],
                    "kuaishou": None,
                    "bilibili": None,
                    "douyin": None,
                    "copy": None,
                },
            )
        )
        entry["source_title"] = nxt["title"]
        entry["url"] = nxt["url"]

        if args.dry_run:
            if entry.get("copy"):
                copy = entry["copy"]
                print(
                    f"next short: {video_id} | source={nxt['title']!r} | "
                    f"copy#{copy.get('index', '?')}={copy.get('title')!r} | "
                    f"ks={bool(entry.get('kuaishou'))} bili={bool(entry.get('bilibili'))}",
                    flush=True,
                )
            else:
                idx = int(state.get("copy_index", 0))
                if idx >= len(copies):
                    idx = 0
                preview = copies[idx]
                print(
                    f"next short: {video_id} | source={nxt['title']!r} | "
                    f"would_use_copy#{idx}={preview['title']!r} | "
                    f"ks={bool(entry.get('kuaishou'))} bili={bool(entry.get('bilibili'))}",
                    flush=True,
                )
            print("dry-run: stop before download/upload")
            break

        print(
            f"candidate {quality_tries}/{DEFAULT_MAX_QUALITY_TRIES}: {video_id} | "
            f"source={nxt['title']!r}",
            flush=True,
        )

        try:
            video_path, source_title = download_highest(
                nxt["url"], video_id, args.inbox, cookies_from_browser=cookies_from_browser
            )
        except QualityRejected as exc:
            mark_quality_rejected(
                state,
                entry,
                video_id=video_id,
                inbox=args.inbox,
                reason=str(exc),
                state_path=args.state,
            )
            continue

        entry["source_title"] = source_title
        entry["downloaded"] = _now_iso()
        if not entry.get("copy"):
            entry["copy"] = allocate_copy(state, copies)
        _save_state(args.state, state)

        copy = entry["copy"]
        publish_title = copy["title"]
        publish_desc = copy.get("desc") or publish_title
        print(f"downloaded: {video_path}", flush=True)
        print(
            f"publish_title={publish_title!r} | copy#{copy.get('index', '?')}",
            flush=True,
        )

        try:
            if not entry.get("kuaishou"):
                upload_kuaishou(
                    sau, args.account, video_path, publish_title, publish_desc, args.ks_tags
                )
                entry["kuaishou"] = _now_iso()
                _save_state(args.state, state)
                print(f"kuaishou ok: {video_id}", flush=True)

            if not entry.get("bilibili"):
                upload_bilibili(
                    sau,
                    args.account,
                    video_path,
                    publish_title,
                    publish_desc,
                    args.bili_tid,
                    args.bili_tags,
                )
                entry["bilibili"] = _now_iso()
                _save_state(args.state, state)
                print(f"bilibili ok: {video_id}", flush=True)
        finally:
            if is_done(entry):
                cleanup_local(args.inbox, video_id)
            _save_state(args.state, state)

        if not is_done(entry):
            print(f"短视频 {video_id} 未完全成功，保留本地文件供重试", file=sys.stderr)
            return 5

        processed += 1

    _save_state(args.state, state)
    print(f"done, processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
