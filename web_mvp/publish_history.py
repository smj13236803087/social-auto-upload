"""Publish history: JSON (user UI) + MySQL (admin / analytics)."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from conf import BASE_DIR

STORE_PATH = Path(BASE_DIR) / "data" / "publish_history.json"
BACKFILL_FLAG = Path(BASE_DIR) / "data" / ".publish_history_db_backfilled"
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


def _parse_created_at(raw: str | None) -> datetime:
    text = (raw or "").strip()
    if not text:
        return datetime.now()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return datetime.now()


def _db_insert_event(event: dict, *, user_id: int | None) -> None:
    from web_mvp.auth import db_conn

    created = _parse_created_at(event.get("created_at"))
    results = list(event.get("results") or [])
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO publish_events
                  (id, user_id, source, source_label, title, source_ref, status, detail, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  user_id=VALUES(user_id),
                  source=VALUES(source),
                  source_label=VALUES(source_label),
                  title=VALUES(title),
                  source_ref=VALUES(source_ref),
                  status=VALUES(status),
                  detail=VALUES(detail),
                  created_at=VALUES(created_at)
                """,
                (
                    event["id"],
                    int(user_id) if user_id else None,
                    (event.get("source") or "")[:32],
                    (event.get("source_label") or "")[:64],
                    (event.get("title") or "")[:200],
                    (event.get("source_ref") or "")[:300],
                    (event.get("status") or "failed")[:16],
                    (event.get("detail") or "")[:500],
                    created,
                ),
            )
            cur.execute("DELETE FROM publish_event_targets WHERE event_id=%s", (event["id"],))
            for r in results:
                cur.execute(
                    """
                    INSERT INTO publish_event_targets
                      (event_id, platform, account, ok, error)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        event["id"],
                        str(r.get("platform") or "")[:32],
                        str(r.get("account") or "")[:128],
                        1 if r.get("ok") else 0,
                        str(r.get("error") or "")[:500],
                    ),
                )


def ensure_db_backfill() -> None:
    """One-shot import of legacy JSON history into MySQL."""
    if BACKFILL_FLAG.exists():
        return
    with _lock:
        if BACKFILL_FLAG.exists():
            return
        items = load_history()
        for event in reversed(items):
            try:
                _db_insert_event(event, user_id=event.get("user_id"))
            except Exception:
                # Table may not exist yet; retry next boot after migration.
                return
        try:
            BACKFILL_FLAG.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        except OSError:
            pass


def append_event(
    *,
    source: str,
    title: str = "",
    results: list[dict] | None = None,
    status: str | None = None,
    detail: str = "",
    source_ref: str = "",
    user_id: int | None = None,
) -> dict:
    """Record one publish attempt (or skip). Newest first in JSON; all rows kept in MySQL."""
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
        "user_id": int(user_id) if user_id else None,
    }
    with _lock:
        items = load_history()
        items.insert(0, event)
        save_history(items[:MAX_ITEMS])
    try:
        _db_insert_event(event, user_id=user_id)
    except Exception as exc:
        print(f"[publish_history] db write failed: {exc}", flush=True)
    return event


def list_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), MAX_ITEMS))
    with _lock:
        return load_history()[:limit]


def admin_list_events(
    *,
    q: str = "",
    status: str = "",
    user_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    from web_mvp.auth import db_conn

    ensure_db_backfill()
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    where = ["1=1"]
    args: list[Any] = []
    text = (q or "").strip()
    if text:
        where.append("(e.title LIKE %s OR e.source_ref LIKE %s OR u.email LIKE %s OR u.username LIKE %s)")
        like = f"%{text}%"
        args.extend([like, like, like, like])
    if status in ("success", "partial", "failed", "skipped"):
        where.append("e.status=%s")
        args.append(status)
    if user_id:
        where.append("e.user_id=%s")
        args.append(int(user_id))
    clause = " AND ".join(where)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM publish_events e
                LEFT JOIN users u ON u.id = e.user_id
                WHERE {clause}
                """,
                args,
            )
            total = int((cur.fetchone() or {}).get("c") or 0)
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT e.id, e.user_id, e.source, e.source_label, e.title, e.source_ref,
                       e.status, e.detail, e.created_at,
                       u.email AS user_email, u.username AS user_name
                FROM publish_events e
                LEFT JOIN users u ON u.id = e.user_id
                WHERE {clause}
                ORDER BY e.created_at DESC
                LIMIT %s OFFSET %s
                """,
                [*args, page_size, offset],
            )
            rows = cur.fetchall() or []
            event_ids = [r["id"] for r in rows]
            targets_by_event: dict[str, list] = {eid: [] for eid in event_ids}
            if event_ids:
                placeholders = ",".join(["%s"] * len(event_ids))
                cur.execute(
                    f"""
                    SELECT event_id, platform, account, ok, error
                    FROM publish_event_targets
                    WHERE event_id IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    event_ids,
                )
                for t in cur.fetchall() or []:
                    targets_by_event.setdefault(t["event_id"], []).append(
                        {
                            "platform": t.get("platform") or "",
                            "account": t.get("account") or "",
                            "ok": bool(t.get("ok")),
                            "error": t.get("error") or "",
                        }
                    )
    events = []
    for r in rows:
        created = r.get("created_at")
        events.append(
            {
                "id": r["id"],
                "user_id": int(r["user_id"]) if r.get("user_id") else None,
                "user_email": r.get("user_email") or "",
                "user_name": r.get("user_name") or "",
                "source": r.get("source") or "",
                "source_label": r.get("source_label") or "",
                "title": r.get("title") or "",
                "source_ref": r.get("source_ref") or "",
                "status": r.get("status") or "",
                "detail": r.get("detail") or "",
                "created_at": created.isoformat(timespec="seconds")
                if hasattr(created, "isoformat")
                else str(created or ""),
                "results": targets_by_event.get(r["id"]) or [],
            }
        )
    return {"ok": True, "total": total, "page": page, "page_size": page_size, "events": events}
