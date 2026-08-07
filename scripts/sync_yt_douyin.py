#!/usr/bin/env python3
"""Daily sync: YouTube Shorts (newest first) -> Douyin only.

Independent schedule from Kuaishou + Bilibili.

Rules (Beijing time):
- Schedule 10:00 / 17:00 / 22:30, 1 short per run (= 3/day)
- Same channel/copy/claim pool as food pipeline (shared state)
- Prefer H.264 1080p download
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_yt_food as food  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync YouTube Shorts to Douyin")
    parser.add_argument("--channel", default=food.DEFAULT_CHANNEL)
    parser.add_argument("--account", default=food.DEFAULT_ACCOUNT)
    parser.add_argument("--daily-limit", type=int, default=1)
    parser.add_argument("--lookback", type=int, default=50)
    parser.add_argument("--state", type=Path, default=food.DEFAULT_STATE)
    parser.add_argument("--inbox", type=Path, default=food.DEFAULT_INBOX)
    parser.add_argument("--copy-library", type=Path, default=food.DEFAULT_COPY_LIBRARY)
    parser.add_argument("--cookies-from-browser", default=food.DEFAULT_COOKIES_FROM_BROWSER)
    parser.add_argument("--dy-tags", default=food.DEFAULT_DY_TAGS, help="Douyin tags without #")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = food._load_state(args.state)
    state["channel"] = args.channel
    copies = food._load_copy_library(args.copy_library)
    cookies_from_browser = (args.cookies_from_browser or "").strip() or None

    sau = food._venv_bin("sau")
    print(f"checking cookies for account={args.account!r} (douyin)", flush=True)
    dy_ok = food.check_platform(sau, "douyin", args.account)
    if not dy_ok:
        print(
            "cookie 无效，请先在本机执行：\n"
            f'  sau douyin login --account "{args.account}"',
            file=sys.stderr,
        )
        return 3

    print(f"listing shorts from {args.channel} (lookback={args.lookback})", flush=True)
    items = food.list_shorts(args.channel, args.lookback, cookies_from_browser=cookies_from_browser)
    if not items:
        print("未获取到 Shorts 列表", file=sys.stderr)
        return 4

    processed = 0
    quality_tries = 0
    while processed < args.daily_limit:
        if quality_tries >= food.DEFAULT_MAX_QUALITY_TRIES:
            print(
                f"已试 {food.DEFAULT_MAX_QUALITY_TRIES} 个视频清晰度都不达标，本次发布任务停止",
                file=sys.stderr,
            )
            return 7

        nxt = food.pick_next(items, state)
        if not nxt:
            msg = "lookback 范围内没有未使用的新视频（抖音），本次未上传"
            print(msg, file=sys.stderr)
            if processed == 0:
                return 6
            break

        video_id = nxt["id"]
        quality_tries += 1
        entry = food._normalize_entry(
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
                title = entry["copy"].get("title")
            else:
                idx = int(state.get("copy_index", 0))
                if idx >= len(copies):
                    idx = 0
                title = copies[idx]["title"]
            print(
                f"next short: {video_id} | source={nxt['title']!r} | would_use={title!r} | "
                f"dy={bool(entry.get('douyin'))}",
                flush=True,
            )
            print("dry-run: stop before download/upload")
            break

        print(
            f"candidate {quality_tries}/{food.DEFAULT_MAX_QUALITY_TRIES}: {video_id} | "
            f"source={nxt['title']!r}",
            flush=True,
        )

        try:
            video_path, source_title = food.download_highest(
                nxt["url"], video_id, args.inbox, cookies_from_browser=cookies_from_browser
            )
        except food.QualityRejected as exc:
            food.mark_quality_rejected(
                state,
                entry,
                video_id=video_id,
                inbox=args.inbox,
                reason=str(exc),
                state_path=args.state,
            )
            continue

        entry["source_title"] = source_title
        entry["downloaded"] = food._now_iso()
        if not entry.get("copy"):
            entry["copy"] = food.allocate_copy(state, copies)
        food._save_state(args.state, state)

        copy = entry["copy"]
        publish_title = copy["title"]
        publish_desc = copy.get("desc") or publish_title
        print(f"downloaded: {video_path}", flush=True)
        print(
            f"publish_title={publish_title!r} | copy#{copy.get('index', '?')}",
            flush=True,
        )

        try:
            if not entry.get("douyin"):
                food.upload_douyin(
                    sau, args.account, video_path, publish_title, publish_desc, args.dy_tags
                )
                entry["douyin"] = food._now_iso()
                food._save_state(args.state, state)
                print(f"douyin ok: {video_id}", flush=True)
        finally:
            if entry.get("douyin"):
                food.cleanup_local(args.inbox, video_id)
            food._save_state(args.state, state)

        if not entry.get("douyin"):
            print(f"短视频 {video_id} 抖音未成功，保留本地文件供重试", file=sys.stderr)
            return 5

        processed += 1

    food._save_state(args.state, state)
    print(f"done, processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
