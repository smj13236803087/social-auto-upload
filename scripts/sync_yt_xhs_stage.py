#!/usr/bin/env python3
"""Download Shorts to iCloud for manual Xiaohongshu posting (phone Files app).

Independent from Kuaishou/Bilibili/Douyin auto-upload.

Rules (Beijing time):
- Starts 2026-08-14
- Schedule 08:00 and 20:00, 1 short per run (= 2/day)
- Same channel/copy/claim rules as food pipeline (shared state)
- Stage video + 文案.txt to iCloud sau-xhs-待发
- At each run, delete staged folders from previous calendar days
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_yt_food as food  # noqa: E402

SCHEDULE_START_DATE = date(2026, 8, 14)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage YouTube Shorts to iCloud for manual Xiaohongshu")
    parser.add_argument("--channel", default=food.DEFAULT_CHANNEL)
    parser.add_argument("--lookback", type=int, default=food.DEFAULT_LOOKBACK)
    parser.add_argument("--daily-limit", type=int, default=1)
    parser.add_argument("--state", type=Path, default=food.DEFAULT_STATE)
    parser.add_argument("--inbox", type=Path, default=food.DEFAULT_INBOX)
    parser.add_argument("--copy-library", type=Path, default=food.DEFAULT_COPY_LIBRARY)
    parser.add_argument("--cookies-from-browser", default=food.DEFAULT_COOKIES_FROM_BROWSER)
    parser.add_argument("--xhs-tags", default=food.DEFAULT_XHS_TAGS)
    parser.add_argument("--xhs-stage-dir", type=Path, default=food.DEFAULT_XHS_STAGE_DIR)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore schedule start date (2026-08-14)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = food._beijing_now().date()
    if not args.force and today < SCHEDULE_START_DATE:
        print(
            f"xhs iCloud staging starts {SCHEDULE_START_DATE.isoformat()} Asia/Shanghai; "
            f"today={today.isoformat()}, skip (use --force to run anyway)",
            flush=True,
        )
        return 0

    state = food._load_state(args.state)
    state["channel"] = args.channel
    copies = food._load_copy_library(args.copy_library)
    cookies_from_browser = (args.cookies_from_browser or "").strip() or None

    if not args.dry_run:
        food.cleanup_previous_day_xhs_stages(args.xhs_stage_dir, today)

    print(f"listing shorts from {args.channel} (lookback={args.lookback})", flush=True)
    items, _probe = food.list_shorts_until_pick(
        args.channel,
        state,
        ("xhs_staged",),
        lookback=args.lookback,
        cookies_from_browser=cookies_from_browser,
    )
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

        nxt = food.pick_next_for(items, state, ("xhs_staged",))
        if not nxt:
            items, nxt = food.list_shorts_until_pick(
                args.channel,
                state,
                ("xhs_staged",),
                lookback=args.lookback,
                cookies_from_browser=cookies_from_browser,
            )
        if not nxt:
            msg = "扩大回看后仍没有可发的新视频（小红书云盘待发），本次未下载"
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
                f"next short: {video_id} | source={nxt['title']!r} | would_use={title!r}",
                flush=True,
            )
            print("dry-run: stop before download/stage")
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
            f"stage_title={publish_title!r} | copy#{copy.get('index', '?')}",
            flush=True,
        )

        food.stage_for_xhs_manual(
            video_path,
            video_id=video_id,
            title=publish_title,
            desc=publish_desc,
            tags=args.xhs_tags,
            stage_dir=args.xhs_stage_dir,
        )
        entry["xhs_staged"] = food._now_iso()
        food._save_state(args.state, state)

        food.cleanup_local(args.inbox, video_id)
        processed += 1

    food._save_state(args.state, state)
    print(f"done, processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
