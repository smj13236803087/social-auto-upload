"""Daily creator homepage sync: download latest video and upload to target platforms."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from conf import BASE_DIR
from downloader.cn_share import detect_platform, download_share_url, extract_url, fetch_latest_video_from_profile
from web_mvp.bili_partitions import DEFAULT_BILIBILI_TID
from web_mvp import publish_history as history_store
from web_mvp.targets import targets_from_record, targets_summary

STORE_PATH = Path(BASE_DIR) / "data" / "creator_subscriptions.json"
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
MAX_SUBSCRIPTIONS = 2

_lock = threading.Lock()
_scheduler_started = False
_running_ids: set[str] = set()

# Injected by app to avoid circular imports for upload.
_publish_fn: Callable | None = None


def set_publish_fn(fn: Callable) -> None:
    global _publish_fn
    _publish_fn = fn


def load_subscriptions() -> list[dict]:
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_subscriptions(items: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_daily_time(raw: str) -> str:
    text = (raw or "").strip()
    match = _TIME_RE.match(text)
    if not match:
        raise ValueError("每天时间格式应为 HH:MM，例如 09:00")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def add_subscription(
    *,
    homepage_url: str,
    daily_time: str,
    targets: list[dict],
    tid: int = DEFAULT_BILIBILI_TID,
) -> dict:
    url = extract_url(homepage_url)
    source_platform = detect_platform(url)
    normalized = targets_from_record({"targets": targets, "tid": tid})
    if not normalized:
        raise ValueError("请至少选择一个上传目标账号")

    # Probe the homepage before saving — reject dead / wrong links early.
    try:
        latest = fetch_latest_video_from_profile(url)
    except Exception as exc:
        raise ValueError(
            f"主页链接无效或暂时无法获取作品：{exc}"
        ) from exc
    if not latest or not latest.video_id:
        raise ValueError("主页链接无法解析到任何作品，请确认是博主主页链接")

    primary = normalized[0]
    item = {
        "id": uuid.uuid4().hex,
        "homepage_url": url,
        "source_platform": source_platform,
        "daily_time": normalize_daily_time(daily_time),
        "targets": normalized,
        # Legacy fields kept for older UI / scripts
        "upload_platform": primary["platform"],
        "upload_account": primary["account"],
        "tid": int(tid or DEFAULT_BILIBILI_TID),
        "enabled": True,
        "last_video_id": "",
        "last_run_date": "",
        "last_run_at": "",
        "last_status": "",
        "last_error": "",
        "last_title": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "probe_title": latest.title or "",
        "probe_video_id": latest.video_id or "",
    }
    with _lock:
        items = load_subscriptions()
        if len(items) >= MAX_SUBSCRIPTIONS:
            raise ValueError(f"博主订阅最多 {MAX_SUBSCRIPTIONS} 条，请先删除后再添加")
        items.append(item)
        save_subscriptions(items)
    return item


def delete_subscription(sub_id: str) -> bool:
    with _lock:
        items = load_subscriptions()
        new_items = [x for x in items if x.get("id") != sub_id]
        if len(new_items) == len(items):
            return False
        save_subscriptions(new_items)
    return True


def set_enabled(sub_id: str, enabled: bool) -> dict | None:
    with _lock:
        items = load_subscriptions()
        for item in items:
            if item.get("id") == sub_id:
                item["enabled"] = bool(enabled)
                save_subscriptions(items)
                return item
    return None


def _update_sub(sub_id: str, **fields) -> None:
    with _lock:
        items = load_subscriptions()
        for item in items:
            if item.get("id") == sub_id:
                item.update(fields)
                save_subscriptions(items)
                return


def run_subscription(sub_id: str, *, force: bool = False) -> dict:
    if _publish_fn is None:
        raise RuntimeError("上传函数未初始化")

    with _lock:
        items = load_subscriptions()
        sub = next((x for x in items if x.get("id") == sub_id), None)
    if not sub:
        raise ValueError("订阅不存在")

    if sub_id in _running_ids:
        raise RuntimeError("该订阅正在执行中")
    _running_ids.add(sub_id)

    status_written = False
    try:
        latest = fetch_latest_video_from_profile(sub["homepage_url"])
        if not force and latest.video_id and latest.video_id == sub.get("last_video_id"):
            msg = f"无新视频（最新仍是 {latest.video_id}）"
            _update_sub(
                sub_id,
                last_run_at=datetime.now().isoformat(timespec="seconds"),
                last_run_date=datetime.now().strftime("%Y-%m-%d"),
                last_status="skipped",
                last_error="",
                last_title=latest.title,
            )
            history_store.append_event(
                source="subscription",
                title=latest.title or "",
                status="skipped",
                detail=msg,
                source_ref=sub.get("homepage_url") or "",
            )
            return {"ok": True, "skipped": True, "message": msg, "video_id": latest.video_id}

        targets = targets_from_record(sub)
        if not targets:
            raise RuntimeError("订阅未配置上传目标")

        result = download_share_url(latest.url)
        # Prefer yt-dlp title; browser profile scrape often only has video id.
        publish_title = (result.title or "").strip() or (latest.title or "").strip() or latest.video_id
        if publish_title == latest.video_id and (result.title or "").strip():
            publish_title = result.title.strip()
        upload_results: list[dict] = []
        for target in targets:
            try:
                _publish_fn(
                    platform=target["platform"],
                    account=target["account"],
                    video_path=result.file_path,
                    title=publish_title,
                    description="",
                    tags=[],
                    tid=int(target.get("tid") or sub.get("tid") or DEFAULT_BILIBILI_TID),
                )
                upload_results.append(
                    {"ok": True, "platform": target["platform"], "account": target["account"]}
                )
            except Exception as exc:
                upload_results.append(
                    {
                        "ok": False,
                        "platform": target["platform"],
                        "account": target["account"],
                        "error": str(exc),
                    }
                )

        any_ok = any(r.get("ok") for r in upload_results)
        all_ok = bool(upload_results) and all(r.get("ok") for r in upload_results)
        err_text = "；".join(
            f"{r['platform']}/{r['account']}: {r.get('error')}"
            for r in upload_results
            if not r.get("ok")
        )
        _update_sub(
            sub_id,
            last_video_id=latest.video_id if any_ok else (sub.get("last_video_id") or ""),
            last_run_at=datetime.now().isoformat(timespec="seconds"),
            last_run_date=datetime.now().strftime("%Y-%m-%d"),
            last_status="success" if all_ok else ("partial" if any_ok else "failed"),
            last_error="" if all_ok else err_text[-500:],
            last_title=publish_title,
        )
        status_written = True
        history_store.append_event(
            source="subscription",
            title=publish_title,
            results=upload_results,
            detail="" if all_ok else err_text,
            source_ref=sub.get("homepage_url") or "",
        )
        if not any_ok:
            raise RuntimeError(err_text or "全部目标上传失败")
        return {
            "ok": True,
            "skipped": False,
            "video_id": latest.video_id,
            "title": publish_title,
            "file": str(result.file_path),
            "targets": targets,
            "results": upload_results,
            "targets_summary": targets_summary(targets),
            "upload_platform": targets[0]["platform"],
            "upload_account": targets[0]["account"],
        }
    except Exception as exc:
        if not status_written:
            _update_sub(
                sub_id,
                last_run_at=datetime.now().isoformat(timespec="seconds"),
                last_run_date=datetime.now().strftime("%Y-%m-%d"),
                last_status="failed",
                last_error=str(exc)[-500:],
            )
            history_store.append_event(
                source="subscription",
                title=str(sub.get("last_title") or ""),
                status="failed",
                detail=str(exc)[-500:],
                source_ref=sub.get("homepage_url") or "",
            )
        raise
    finally:
        _running_ids.discard(sub_id)


def _scheduler_loop() -> None:
    while True:
        try:
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            with _lock:
                items = list(load_subscriptions())
            for sub in items:
                if not sub.get("enabled"):
                    continue
                if sub.get("daily_time") != hhmm:
                    continue
                if sub.get("last_run_date") == today:
                    continue
                sub_id = sub.get("id")
                if not sub_id or sub_id in _running_ids:
                    continue
                try:
                    run_subscription(sub_id, force=False)
                except Exception:
                    # Errors already persisted on the subscription record.
                    pass
        except Exception:
            pass
        time.sleep(20)


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    thread = threading.Thread(target=_scheduler_loop, name="creator-sub-scheduler", daemon=True)
    thread.start()
