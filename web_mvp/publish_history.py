"""Append-only publish history for the Web MVP UI."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from conf import BASE_DIR

STORE_PATH = Path(BASE_DIR) / "data" / "publish_history.json"
MAX_ITEMS = 100

_lock = threading.Lock()

SOURCE_LABELS = {
    "link": "贴链接",
    "subscription": "博主订阅",
    "folder": "文件夹队列",
    "publish": "直接上传",
    "local": "本地视频",
}


def _ensure_store() -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STORE_PATH.exists():
        STORE_PATH.write_text("[]", encoding="utf-8")


def load_history() -> list[dict]:
    _ensure_store()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_history(items: list[dict]) -> None:
    _ensure_store()
    STORE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def status_from_results(results: list[dict] | None) -> str:
    results = results or []
    if not results:
        return "skipped"
    any_ok = any(r.get("ok") for r in results)
    all_ok = all(r.get("ok") for r in results)
    if all_ok:
        return "success"
    if any_ok:
        return "partial"
    return "failed"


def append_event(
    *,
    source: str,
    title: str = "",
    results: list[dict] | None = None,
    status: str | None = None,
    detail: str = "",
    source_ref: str = "",
) -> dict:
    """Record one publish attempt (or skip). Newest first, capped at MAX_ITEMS."""
    results = list(results or [])
    event = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "title": (title or "").strip()[:200],
        "source_ref": (source_ref or "").strip()[:300],
        "status": status or status_from_results(results),
        "detail": (detail or "").strip()[:500],
        "results": results,
    }
    with _lock:
        items = load_history()
        items.insert(0, event)
        save_history(items[:MAX_ITEMS])
    return event


def list_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), MAX_ITEMS))
    with _lock:
        return load_history()[:limit]
