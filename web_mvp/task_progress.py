"""Cross-worker task progress (file-backed) for long upload/publish jobs."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from conf import BASE_DIR

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

TASKS_DIR = Path(BASE_DIR) / "data" / "tasks"
_LOCK_PATH = TASKS_DIR / ".lock"
_mem_lock = threading.Lock()
TTL_SECONDS = 3600


class _FileLock:
    def __enter__(self):
        _mem_lock.acquire()
        self._fh = None
        if fcntl is not None:
            TASKS_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = _LOCK_PATH.open("a+", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
        _mem_lock.release()
        return False


def _path(task_id: str) -> Path:
    safe = "".join(c for c in task_id if c.isalnum())
    return TASKS_DIR / f"{safe}.json"


def _read(task_id: str) -> dict[str, Any] | None:
    p = _path(task_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write(task_id: str, data: dict[str, Any]) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(task_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def create_task(message: str = "准备中…") -> str:
    task_id = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    with _FileLock():
        _write(
            task_id,
            {
                "id": task_id,
                "status": "running",
                "pct": 0,
                "message": message,
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            },
        )
    return task_id


def update_task(task_id: str, pct: int, message: str) -> None:
    with _FileLock():
        data = _read(task_id)
        if not data or data.get("status") != "running":
            return
        data["pct"] = max(0, min(100, int(pct)))
        data["message"] = message or data.get("message") or ""
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write(task_id, data)


def complete_task(task_id: str, result: dict[str, Any]) -> None:
    with _FileLock():
        data = _read(task_id) or {"id": task_id}
        data["status"] = "done"
        data["pct"] = 100
        data["message"] = data.get("message") or "完成"
        data["result"] = result
        data["error"] = None
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write(task_id, data)


def fail_task(task_id: str, error: str) -> None:
    with _FileLock():
        data = _read(task_id) or {"id": task_id}
        data["status"] = "error"
        data["message"] = error or "失败"
        data["error"] = error or "失败"
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write(task_id, data)


def get_task(task_id: str) -> dict[str, Any] | None:
    with _FileLock():
        data = _read(task_id)
    if not data:
        return None
    # Soft TTL cleanup of very old tasks when touched
    try:
        created = data.get("created_at") or ""
        # best-effort; ignore parse errors
        if created and (time.time() - Path(_path(task_id)).stat().st_mtime) > TTL_SECONDS:
            pass
    except OSError:
        pass
    return data


def _parse_iso(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def list_tasks(*, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    status = (status or "").strip().lower()
    rows: list[dict[str, Any]] = []
    now = datetime.now()
    with _FileLock():
        files = sorted(TASKS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            if path.name.startswith("."):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            st = str(data.get("status") or "").lower()
            if status and st != status:
                continue
            created = _parse_iso(str(data.get("created_at") or ""))
            updated = _parse_iso(str(data.get("updated_at") or "")) or created
            end = updated or now
            start = created or end
            duration_sec = max(0, int((end - start).total_seconds())) if start and end else 0
            if st == "running" and created:
                duration_sec = max(0, int((now - created).total_seconds()))
            item = dict(data)
            item["duration_sec"] = duration_sec
            item.pop("result", None)
            rows.append(item)
            if len(rows) >= max(1, min(int(limit or 100), 300)):
                break
    return rows
