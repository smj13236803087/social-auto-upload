"""Local folder daily upload queue: oldest-first by creation time, dedupe by filename."""

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
from web_mvp.bili_partitions import DEFAULT_BILIBILI_TID
from web_mvp import publish_history as history_store
from web_mvp.targets import targets_from_record, targets_summary

STORE_PATH = Path(BASE_DIR) / "data" / "folder_queues.json"
DEFAULT_COPY = "奔赴下一场山海"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".flv", ".avi", ".m4v"}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
MAX_FOLDER_QUEUES = 2

_lock = threading.Lock()
_scheduler_started = False
_running_ids: set[str] = set()
_publish_fn: Callable | None = None


def set_publish_fn(fn: Callable) -> None:
    global _publish_fn
    _publish_fn = fn


def _ensure_store() -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STORE_PATH.exists():
        STORE_PATH.write_text("[]", encoding="utf-8")


def load_queues() -> list[dict]:
    _ensure_store()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_queues(items: list[dict]) -> None:
    _ensure_store()
    STORE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_daily_time(raw: str) -> str:
    text = (raw or "").strip()
    match = _TIME_RE.match(text)
    if not match:
        raise ValueError("时间格式应为 HH:MM，例如 09:30")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _created_ts(path: Path) -> float:
    st = path.stat()
    birth = getattr(st, "st_birthtime", None)
    if birth:
        return float(birth)
    return float(st.st_ctime)


def scan_pending_videos(folder_path: str, uploaded_filenames: list[str] | None = None) -> list[dict]:
    folder = Path(folder_path).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"文件夹不存在或不是目录: {folder}")

    uploaded = set(uploaded_filenames or [])
    items: list[dict] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTS:
            continue
        if path.name in uploaded:
            continue
        created = _created_ts(path)
        items.append(
            {
                "filename": path.name,
                "path": str(path),
                "created_ts": created,
                "created_at": datetime.fromtimestamp(created).isoformat(timespec="seconds"),
                "size": path.stat().st_size,
            }
        )
    items.sort(key=lambda x: (x["created_ts"], x["filename"]))
    return items


def _resolve_meta(filename: str, drafts: dict) -> dict:
    draft = (drafts or {}).get(filename) or {}
    title = str(draft.get("title") or "").strip() or DEFAULT_COPY
    description = str(draft.get("description") or "").strip() or DEFAULT_COPY
    tags_raw = draft.get("tags") or ""
    if isinstance(tags_raw, list):
        tags = [str(t).strip().lstrip("#") for t in tags_raw if str(t).strip()]
    else:
        tags = [t.strip().lstrip("#") for t in str(tags_raw).split(",") if t.strip()]
    return {"title": title, "description": description, "tags": tags, "tags_text": ",".join(tags)}


def enrich_queue(item: dict) -> dict:
    out = dict(item)
    try:
        pending = scan_pending_videos(item.get("folder_path") or "", item.get("uploaded_filenames") or [])
    except Exception as exc:
        out["upcoming"] = []
        out["next_video"] = None
        out["pending_count"] = 0
        out["scan_error"] = str(exc)
        return out

    drafts = item.get("drafts") or {}
    upcoming = []
    for video in pending[:5]:
        meta = _resolve_meta(video["filename"], drafts)
        draft = drafts.get(video["filename"]) or {}
        upcoming.append(
            {
                **video,
                "title": meta["title"],
                "description": meta["description"],
                "tags": meta["tags_text"],
                "title_draft": str(draft.get("title") or ""),
                "description_draft": str(draft.get("description") or ""),
                "tags_draft": (
                    ",".join(draft.get("tags"))
                    if isinstance(draft.get("tags"), list)
                    else str(draft.get("tags") or "")
                ),
            }
        )
    out["upcoming"] = upcoming
    out["next_video"] = upcoming[0] if upcoming else None
    out["pending_count"] = len(pending)
    out["scan_error"] = ""
    out["default_copy"] = DEFAULT_COPY
    targets = targets_from_record(item)
    out["targets"] = targets
    out["targets_summary"] = targets_summary(targets)
    return out


def add_queue(
    *,
    folder_path: str,
    daily_time: str,
    targets: list[dict],
    tid: int = DEFAULT_BILIBILI_TID,
) -> dict:
    raw = (folder_path or "").strip()
    if not raw:
        raise ValueError("请先选择要监控的文件夹")
    folder = Path(raw).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"文件夹不存在或不是目录: {folder}")
    # Validate readable
    scan_pending_videos(str(folder), [])

    normalized = targets_from_record({"targets": targets, "tid": tid})
    if not normalized:
        raise ValueError("请至少选择一个上传目标账号")
    primary = normalized[0]

    item = {
        "id": uuid.uuid4().hex,
        "folder_path": str(folder),
        "daily_time": normalize_daily_time(daily_time),
        "targets": normalized,
        "upload_platform": primary["platform"],
        "upload_account": primary["account"],
        "tid": int(tid or DEFAULT_BILIBILI_TID),
        "enabled": True,
        "uploaded_filenames": [],
        "drafts": {},
        "last_run_date": "",
        "last_run_at": "",
        "last_status": "",
        "last_error": "",
        "last_filename": "",
        "last_title": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        items = load_queues()
        if len(items) >= MAX_FOLDER_QUEUES:
            raise ValueError(f"文件夹队列最多 {MAX_FOLDER_QUEUES} 条，请先删除后再添加")
        items.append(item)
        save_queues(items)
    return enrich_queue(item)


def delete_queue(queue_id: str) -> bool:
    with _lock:
        items = load_queues()
        new_items = [x for x in items if x.get("id") != queue_id]
        if len(new_items) == len(items):
            return False
        save_queues(new_items)
    return True


def set_enabled(queue_id: str, enabled: bool) -> dict | None:
    with _lock:
        items = load_queues()
        for item in items:
            if item.get("id") == queue_id:
                item["enabled"] = bool(enabled)
                save_queues(items)
                return enrich_queue(item)
    return None


def update_drafts(queue_id: str, drafts_patch: dict) -> dict | None:
    """Merge drafts for filenames. Empty strings clear the draft field (fall back to default on upload)."""
    with _lock:
        items = load_queues()
        for item in items:
            if item.get("id") != queue_id:
                continue
            drafts = dict(item.get("drafts") or {})
            for filename, payload in (drafts_patch or {}).items():
                if not filename or not isinstance(payload, dict):
                    continue
                current = dict(drafts.get(filename) or {})
                if "title" in payload:
                    current["title"] = str(payload.get("title") or "")
                if "description" in payload:
                    current["description"] = str(payload.get("description") or "")
                if "tags" in payload:
                    tags_val = payload.get("tags")
                    if isinstance(tags_val, list):
                        current["tags"] = ",".join(str(t).strip().lstrip("#") for t in tags_val if str(t).strip())
                    else:
                        current["tags"] = str(tags_val or "")
                drafts[filename] = current
            item["drafts"] = drafts
            save_queues(items)
            return enrich_queue(item)
    return None


def _update_queue(queue_id: str, **fields) -> None:
    with _lock:
        items = load_queues()
        for item in items:
            if item.get("id") == queue_id:
                item.update(fields)
                save_queues(items)
                return


def run_queue(queue_id: str, *, force: bool = False) -> dict:
    if _publish_fn is None:
        raise RuntimeError("上传函数未初始化")

    with _lock:
        items = load_queues()
        queue = next((x for x in items if x.get("id") == queue_id), None)
    if not queue:
        raise ValueError("队列不存在")

    if queue_id in _running_ids:
        raise RuntimeError("该队列正在执行中")
    _running_ids.add(queue_id)

    status_written = False
    try:
        pending = scan_pending_videos(queue["folder_path"], queue.get("uploaded_filenames") or [])
        if not pending:
            msg = "没有待发视频"
            _update_queue(
                queue_id,
                last_run_at=datetime.now().isoformat(timespec="seconds"),
                last_run_date=datetime.now().strftime("%Y-%m-%d"),
                last_status="skipped",
                last_error="",
            )
            history_store.append_event(
                source="folder",
                title="",
                status="skipped",
                detail=msg,
                source_ref=queue.get("folder_path") or "",
            )
            return {"ok": True, "skipped": True, "message": msg}

        next_item = pending[0]
        meta = _resolve_meta(next_item["filename"], queue.get("drafts") or {})
        video_path = Path(next_item["path"])
        if not video_path.exists():
            raise FileNotFoundError(f"文件不存在: {video_path}")

        targets = targets_from_record(queue)
        if not targets:
            raise RuntimeError("队列未配置上传目标")

        upload_results: list[dict] = []
        for target in targets:
            try:
                _publish_fn(
                    platform=target["platform"],
                    account=target["account"],
                    video_path=video_path,
                    title=meta["title"],
                    description=meta["description"],
                    tags=meta["tags"],
                    tid=int(target.get("tid") or queue.get("tid") or DEFAULT_BILIBILI_TID),
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

        uploaded = list(queue.get("uploaded_filenames") or [])
        # Mark filename done if any platform succeeded, to avoid endless retries.
        if any_ok and next_item["filename"] not in uploaded:
            uploaded.append(next_item["filename"])

        _update_queue(
            queue_id,
            uploaded_filenames=uploaded,
            last_run_at=datetime.now().isoformat(timespec="seconds"),
            last_run_date=datetime.now().strftime("%Y-%m-%d"),
            last_status="success" if all_ok else ("partial" if any_ok else "failed"),
            last_error="" if all_ok else err_text[-500:],
            last_filename=next_item["filename"],
            last_title=meta["title"],
        )
        status_written = True
        history_store.append_event(
            source="folder",
            title=meta["title"],
            results=upload_results,
            detail="" if all_ok else err_text,
            source_ref=f"{queue.get('folder_path') or ''} / {next_item['filename']}",
        )
        if not any_ok:
            raise RuntimeError(err_text or "全部目标上传失败")
        return {
            "ok": True,
            "skipped": False,
            "filename": next_item["filename"],
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "targets": targets,
            "results": upload_results,
            "targets_summary": targets_summary(targets),
            "upload_platform": targets[0]["platform"],
            "upload_account": targets[0]["account"],
            "remaining": max(0, len(pending) - 1),
        }
    except Exception as exc:
        if not status_written:
            _update_queue(
                queue_id,
                last_run_at=datetime.now().isoformat(timespec="seconds"),
                last_run_date=datetime.now().strftime("%Y-%m-%d"),
                last_status="failed",
                last_error=str(exc)[-500:],
            )
            history_store.append_event(
                source="folder",
                title=str(queue.get("last_title") or ""),
                status="failed",
                detail=str(exc)[-500:],
                source_ref=queue.get("folder_path") or "",
            )
        raise
    finally:
        _running_ids.discard(queue_id)


def _scheduler_loop() -> None:
    while True:
        try:
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            with _lock:
                items = list(load_queues())
            for queue in items:
                if not queue.get("enabled"):
                    continue
                if queue.get("daily_time") != hhmm:
                    continue
                if queue.get("last_run_date") == today:
                    continue
                qid = queue.get("id")
                if not qid or qid in _running_ids:
                    continue
                try:
                    run_queue(qid, force=False)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(20)


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    thread = threading.Thread(target=_scheduler_loop, name="folder-queue-scheduler", daemon=True)
    thread.start()
