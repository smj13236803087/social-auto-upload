"""Cloud-side job queue for local AutoSelf agents (Douyin etc.)."""

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
except ImportError:  # Windows local agent host may lack fcntl; cloud is Linux
    fcntl = None  # type: ignore

AGENTS_PATH = Path(BASE_DIR) / "data" / "agents.json"
JOBS_PATH = Path(BASE_DIR) / "data" / "agent_jobs.json"
AGENT_ONLINE_SECONDS = 120
# Platforms that should prefer local agent over cloud browser.
AGENT_PLATFORMS = frozenset({"douyin"})

_lock = threading.Lock()
_LOCK_PATH = Path(BASE_DIR) / "data" / "agent_jobs.lock"


class _FileLock:
    """Process-safe lock for dual gunicorn instances sharing JSON stores."""

    def __enter__(self):
        _lock.acquire()
        self._fh = None
        if fcntl is not None:
            _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._fh = _LOCK_PATH.open("a+", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
        _lock.release()
        return False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


def _load(path: Path) -> Any:
    if not path.exists():
        return {} if path == AGENTS_PATH else []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {} if path == AGENTS_PATH else []
    return data


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def heartbeat(*, user_id: int, agent_id: str, label: str = "") -> dict:
    with _FileLock():
        agents = _load(AGENTS_PATH)
        if not isinstance(agents, dict):
            agents = {}
        key = str(int(user_id))
        entry = agents.get(key) or {}
        entry.update(
            {
                "user_id": int(user_id),
                "agent_id": agent_id,
                "label": label or entry.get("label") or "本机助手",
                "last_seen_ts": _now_ts(),
                "last_seen": _now(),
            }
        )
        agents[key] = entry
        _save(AGENTS_PATH, agents)
        return dict(entry)


def get_agent(user_id: int) -> dict | None:
    with _FileLock():
        agents = _load(AGENTS_PATH)
        if not isinstance(agents, dict):
            return None
        entry = agents.get(str(int(user_id)))
        return dict(entry) if isinstance(entry, dict) else None


def is_agent_online(user_id: int) -> bool:
    agent = get_agent(user_id)
    if not agent:
        return False
    last = float(agent.get("last_seen_ts") or 0)
    return (_now_ts() - last) <= AGENT_ONLINE_SECONDS


def agent_status(user_id: int) -> dict:
    agent = get_agent(user_id)
    online = is_agent_online(user_id)
    return {
        "online": online,
        "agent": agent if online else None,
        "platforms": sorted(AGENT_PLATFORMS),
    }


def create_job(
    *,
    user_id: int,
    job_type: str,
    platform: str,
    account: str,
    payload: dict | None = None,
) -> dict:
    job = {
        "id": uuid.uuid4().hex,
        "user_id": int(user_id),
        "type": job_type,
        "platform": platform,
        "account": account,
        "payload": payload or {},
        "status": "pending",
        "qrcode": None,
        "events": [],
        "result": None,
        "error": "",
        "created_at": _now(),
        "updated_at": _now(),
        "created_ts": _now_ts(),
        "updated_ts": _now_ts(),
    }
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            jobs = []
        jobs.append(job)
        # keep last 200
        jobs = jobs[-200:]
        _save(JOBS_PATH, jobs)
    return dict(job)


def get_job(job_id: str) -> dict | None:
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            return None
        for job in jobs:
            if job.get("id") == job_id:
                return dict(job)
    return None


def _update_job(job_id: str, **fields: Any) -> dict | None:
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            return None
        for i, job in enumerate(jobs):
            if job.get("id") != job_id:
                continue
            job.update(fields)
            job["updated_at"] = _now()
            job["updated_ts"] = _now_ts()
            jobs[i] = job
            _save(JOBS_PATH, jobs)
            return dict(job)
    return None


def claim_next_job(*, user_id: int, agent_id: str) -> dict | None:
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            return None
        # 发布优先于登录，避免扫码任务堵住发视频
        pending_idxs = [
            i
            for i, job in enumerate(jobs)
            if int(job.get("user_id") or 0) == int(user_id) and job.get("status") == "pending"
        ]
        if not pending_idxs:
            return None
        publish_idxs = [i for i in pending_idxs if jobs[i].get("type") == "publish"]
        i = publish_idxs[0] if publish_idxs else pending_idxs[0]
        job = jobs[i]
        job["status"] = "running"
        job["agent_id"] = agent_id
        job["updated_at"] = _now()
        job["updated_ts"] = _now_ts()
        jobs[i] = job
        _save(JOBS_PATH, jobs)
        return dict(job)


def set_job_qrcode(job_id: str, qrcode: dict) -> dict | None:
    # 勿把已取消/失败的任务又改回 awaiting_scan（连点扫码时的竞态）
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            return None
        for i, job in enumerate(jobs):
            if job.get("id") != job_id:
                continue
            if job.get("status") in {"cancelled", "error", "done"}:
                return dict(job)
            job["qrcode"] = qrcode
            job["status"] = "awaiting_scan"
            job["updated_at"] = _now()
            job["updated_ts"] = _now_ts()
            jobs[i] = job
            _save(JOBS_PATH, jobs)
            return dict(job)
    return None


def append_job_event(job_id: str, event: dict) -> dict | None:
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            return None
        for i, job in enumerate(jobs):
            if job.get("id") != job_id:
                continue
            if job.get("status") in {"cancelled", "error", "done"}:
                return dict(job)
            events = list(job.get("events") or [])
            item = dict(event or {})
            item["ts"] = _now_ts()
            events.append(item)
            job["events"] = events[-50:]
            job["updated_at"] = _now()
            job["updated_ts"] = _now_ts()
            jobs[i] = job
            _save(JOBS_PATH, jobs)
            return dict(job)
    return None



def set_job_verify_code(job_id: str, code: str) -> dict | None:
    return _update_job(job_id, verify_code=str(code or "").strip())


def find_active_job(
    *,
    user_id: int,
    platform: str | None = None,
    account: str | None = None,
    job_type: str | None = None,
) -> dict | None:
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            return None
        for job in reversed(jobs):
            if int(job.get("user_id") or 0) != int(user_id):
                continue
            if job.get("status") not in {"pending", "running", "awaiting_scan"}:
                continue
            if platform and job.get("platform") != platform:
                continue
            if account and job.get("account") != account:
                continue
            if job_type and job.get("type") != job_type:
                continue
            return dict(job)
    return None


def complete_job(job_id: str, result: dict | None = None) -> dict | None:
    return _update_job(job_id, status="done", result=result or {}, error="")


def fail_job(job_id: str, error: str) -> dict | None:
    return _update_job(job_id, status="error", error=str(error or "失败")[:500])


def cancel_pending_jobs(*, user_id: int, platform: str | None = None, job_type: str | None = None) -> int:
    cancelled = 0
    with _FileLock():
        jobs = _load(JOBS_PATH)
        if not isinstance(jobs, list):
            return 0
        for i, job in enumerate(jobs):
            if int(job.get("user_id") or 0) != int(user_id):
                continue
            if job.get("status") not in {"pending", "running", "awaiting_scan"}:
                continue
            if platform and job.get("platform") != platform:
                continue
            if job_type and job.get("type") != job_type:
                continue
            job["status"] = "cancelled"
            job["error"] = "已被新任务替换"
            job["updated_at"] = _now()
            job["updated_ts"] = _now_ts()
            jobs[i] = job
            cancelled += 1
        if cancelled:
            _save(JOBS_PATH, jobs)
    return cancelled


def list_agents() -> list[dict]:
    with _FileLock():
        agents = _load(AGENTS_PATH)
    if not isinstance(agents, dict):
        return []
    now_ts = _now_ts()
    rows: list[dict] = []
    for entry in agents.values():
        if not isinstance(entry, dict):
            continue
        last = float(entry.get("last_seen_ts") or 0)
        if not last and entry.get("last_seen"):
            try:
                last = datetime.fromisoformat(str(entry["last_seen"])).timestamp()
            except ValueError:
                last = 0
        online = bool(last and (now_ts - last) <= AGENT_ONLINE_SECONDS)
        rows.append(
            {
                "user_id": int(entry.get("user_id") or 0),
                "agent_id": entry.get("agent_id") or "",
                "label": entry.get("label") or "本机助手",
                "last_seen": entry.get("last_seen") or "",
                "last_seen_ts": last,
                "online": online,
                "offline_for_sec": int(max(0, now_ts - last)) if last else None,
            }
        )
    rows.sort(key=lambda r: (not r["online"], -(r.get("last_seen_ts") or 0)))
    return rows


def list_jobs(
    *,
    status: str = "",
    job_type: str = "",
    user_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    with _FileLock():
        jobs = _load(JOBS_PATH)
    if not isinstance(jobs, list):
        return []
    status = (status or "").strip().lower()
    job_type = (job_type or "").strip().lower()
    out: list[dict] = []
    for job in reversed(jobs):
        if not isinstance(job, dict):
            continue
        if user_id is not None and int(job.get("user_id") or 0) != int(user_id):
            continue
        if status and str(job.get("status") or "").lower() != status:
            continue
        if job_type and str(job.get("type") or "").lower() != job_type:
            continue
        item = dict(job)
        # Trim bulky fields for admin list
        events = list(item.get("events") or [])
        item["events_count"] = len(events)
        item["last_event"] = events[-1] if events else None
        item.pop("events", None)
        item.pop("payload", None)
        item.pop("qrcode", None)
        out.append(item)
        if len(out) >= max(1, min(int(limit or 100), 300)):
            break
    return out


def cancel_job(job_id: str) -> dict | None:
    job = get_job(job_id)
    if not job:
        return None
    if job.get("status") in {"done", "error", "cancelled"}:
        return job
    return _update_job(job_id, status="cancelled", error="管理员取消")
