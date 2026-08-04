#!/usr/bin/env python3
"""Daily sync: YouTube Shorts (newest first) -> Xiaohongshu + Kuaishou.

Rules:
- Shorts only (default channel @kochiasmr/shorts)
- 1 short per run (schedule twice daily = 2/day)
- Highest quality download (bv*+ba)
- Skip shorts already uploaded to BOTH Xiaohongshu and Kuaishou
- Chinese copy from food_process_copy_library.json in order
- Delete local files after successful uploads for that short
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANNEL = "https://www.youtube.com/@kochiasmr/shorts"
DEFAULT_ACCOUNT = "boss rabbit"
DEFAULT_STATE = ROOT / "data" / "yt_xhs_sync_state.json"
DEFAULT_INBOX = ROOT / "videos" / "yt_xhs_inbox"
DEFAULT_COPY_LIBRARY = ROOT / "data" / "food_process_copy_library.json"
DEFAULT_COOKIES_FROM_BROWSER = "edge"
DEFAULT_XHS_TAGS = "美食,制作过程,治愈,跟做"
DEFAULT_KS_TAGS = "沉浸式"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"channel": "", "copy_index": 0, "items": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("copy_index", 0)
    data.setdefault("items", {})
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
    if not entry:
        return False
    return bool(entry.get("xiaohongshu")) and bool(entry.get("kuaishou"))


def pick_next(items: list[dict], state: dict) -> dict | None:
    records = state.setdefault("items", {})
    for item in items:
        if not is_done(records.get(item["id"])):
            return item
    return None


def download_highest(
    url: str,
    video_id: str,
    inbox: Path,
    cookies_from_browser: str | None = None,
) -> tuple[Path, str]:
    inbox.mkdir(parents=True, exist_ok=True)
    for p in inbox.glob(f"*{video_id}*"):
        p.unlink(missing_ok=True)

    yt_dlp = shutil.which("yt-dlp") or "yt-dlp"
    out_tmpl = str(inbox / f"xhs_%(upload_date)s_%(id)s_%(title).80B.%(ext)s")
    _run(
        [
            yt_dlp,
            "--no-update",
            *_yt_dlp_cookie_args(cookies_from_browser),
            "-f",
            "bv*+ba/b",
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
    video_path = videos[0]

    title = video_id
    infos = sorted(inbox.glob(f"*{video_id}*.info.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for info_path in infos:
        try:
            data = json.loads(info_path.read_text(encoding="utf-8"))
            if data.get("_type") == "playlist":
                continue
            if data.get("id") == video_id and data.get("title"):
                title = data["title"]
                break
        except Exception:
            continue
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


def upload_xiaohongshu(sau: str, account: str, video: Path, title: str, desc: str, tags: str) -> None:
    _run(
        [
            sau,
            "xiaohongshu",
            "upload-video",
            "--account",
            account,
            "--file",
            str(video),
            "--title",
            title[:20],
            "--desc",
            desc[:1000],
            "--tags",
            tags,
        ]
    )


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


def cleanup_local(inbox: Path, video_id: str) -> None:
    for p in inbox.glob(f"*{video_id}*"):
        p.unlink(missing_ok=True)
        print(f"deleted {p}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync YouTube Shorts to Xiaohongshu + Kuaishou")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--daily-limit", type=int, default=1)
    parser.add_argument("--lookback", type=int, default=50)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--copy-library", type=Path, default=DEFAULT_COPY_LIBRARY)
    parser.add_argument("--cookies-from-browser", default=DEFAULT_COOKIES_FROM_BROWSER)
    parser.add_argument("--tags", default=DEFAULT_XHS_TAGS, help="Xiaohongshu tags")
    parser.add_argument("--ks-tags", default=DEFAULT_KS_TAGS, help="Kuaishou tags without #")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mark-uploaded",
        nargs=2,
        metavar=("VIDEO_ID", "PLATFORM"),
        help="Manually mark uploaded: platform=xiaohongshu|kuaishou",
    )
    args = parser.parse_args()

    state = _load_state(args.state)
    state["channel"] = args.channel
    copies = _load_copy_library(args.copy_library)
    cookies_from_browser = (args.cookies_from_browser or "").strip() or None

    if args.mark_uploaded:
        video_id, platform = args.mark_uploaded
        platform = platform.lower()
        if platform not in {"xiaohongshu", "kuaishou"}:
            print("PLATFORM 只能是 xiaohongshu 或 kuaishou", file=sys.stderr)
            return 2
        entry = state.setdefault("items", {}).setdefault(
            video_id,
            {"source_title": "", "url": "", "xiaohongshu": None, "kuaishou": None, "copy": None},
        )
        entry[platform] = _now_iso()
        _save_state(args.state, state)
        print(f"marked {video_id} -> {platform}")
        return 0

    sau = _venv_bin("sau")
    print(f"checking cookies for account={args.account!r}", flush=True)
    xhs_ok = check_platform(sau, "xiaohongshu", args.account)
    ks_ok = check_platform(sau, "kuaishou", args.account)
    if not xhs_ok or not ks_ok:
        print(
            "cookie 无效，请先在本机执行：\n"
            f'  sau xiaohongshu login --account "{args.account}"\n'
            f'  sau kuaishou login --account "{args.account}"',
            file=sys.stderr,
        )
        return 3

    print(f"listing shorts from {args.channel} (lookback={args.lookback})", flush=True)
    items = list_shorts(args.channel, args.lookback, cookies_from_browser=cookies_from_browser)
    if not items:
        print("未获取到 Shorts 列表", file=sys.stderr)
        return 4

    processed = 0
    while processed < args.daily_limit:
        nxt = pick_next(items, state)
        if not nxt:
            print("lookback 范围内已全部传完（小红书+快手）")
            break

        video_id = nxt["id"]
        entry = state.setdefault("items", {}).setdefault(
            video_id,
            {
                "source_title": nxt["title"],
                "url": nxt["url"],
                "xiaohongshu": None,
                "kuaishou": None,
                "copy": None,
            },
        )
        entry.setdefault("kuaishou", None)
        entry["source_title"] = nxt["title"]
        entry["url"] = nxt["url"]

        if args.dry_run:
            if entry.get("copy"):
                copy = entry["copy"]
                print(
                    f"next short: {video_id} | source={nxt['title']!r} | "
                    f"copy#{copy.get('index', '?')}={copy.get('title')!r} | "
                    f"xhs={bool(entry.get('xiaohongshu'))} ks={bool(entry.get('kuaishou'))}",
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
                    f"xhs={bool(entry.get('xiaohongshu'))} ks={bool(entry.get('kuaishou'))}",
                    flush=True,
                )
            print("dry-run: stop before download/upload")
            break

        if not entry.get("copy"):
            entry["copy"] = allocate_copy(state, copies)
            _save_state(args.state, state)

        copy = entry["copy"]
        publish_title = copy["title"]
        publish_desc = copy.get("desc") or publish_title
        print(
            f"next short: {video_id} | source={nxt['title']!r} | "
            f"copy#{copy.get('index', '?')}={publish_title!r}",
            flush=True,
        )

        video_path, source_title = download_highest(
            nxt["url"], video_id, args.inbox, cookies_from_browser=cookies_from_browser
        )
        entry["source_title"] = source_title
        print(f"downloaded: {video_path}", flush=True)
        print(f"publish_title={publish_title}", flush=True)

        try:
            if not entry.get("xiaohongshu"):
                upload_xiaohongshu(
                    sau, args.account, video_path, publish_title, publish_desc, args.tags
                )
                entry["xiaohongshu"] = _now_iso()
                _save_state(args.state, state)
                print(f"xiaohongshu ok: {video_id}", flush=True)

            if not entry.get("kuaishou"):
                upload_kuaishou(
                    sau, args.account, video_path, publish_title, publish_desc, args.ks_tags
                )
                entry["kuaishou"] = _now_iso()
                _save_state(args.state, state)
                print(f"kuaishou ok: {video_id}", flush=True)
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
