"""Admin dashboard aggregates."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from web_mvp import agent_jobs as agent_store
from web_mvp import publish_history as history_store
from web_mvp import task_progress as task_store


def _day_start(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _user_map(user_ids: set[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({int(x) for x in user_ids if int(x or 0) > 0})
    if not ids:
        return {}
    from web_mvp.auth import db_conn

    placeholders = ",".join(["%s"] * len(ids))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, email, username, role, status FROM users WHERE id IN ({placeholders})",
                ids,
            )
            rows = cur.fetchall() or []
    return {
        int(r["id"]): {
            "id": int(r["id"]),
            "email": r.get("email") or "",
            "username": r.get("username") or "",
            "role": r.get("role") or "user",
            "status": r.get("status") or "",
        }
        for r in rows
    }


def _attach_users(items: list[dict[str, Any]], *, id_key: str = "user_id") -> list[dict[str, Any]]:
    umap = _user_map({int(it.get(id_key) or 0) for it in items})
    out = []
    for it in items:
        row = dict(it)
        u = umap.get(int(row.get(id_key) or 0))
        row["user"] = u
        row["user_email"] = (u or {}).get("email") or ""
        row["user_name"] = (u or {}).get("username") or ""
        out.append(row)
    return out


def overview() -> dict[str, Any]:
    from web_mvp.auth import db_conn

    history_store.ensure_db_backfill()
    # Best-effort: discover cookie files so account counts aren't stuck at 0.
    try:
        from web_mvp import platform_accounts as account_store
        from web_mvp.app import _list_accounts

        account_store.sync_from_cookie_files(_list_accounts(refresh_profile=False), user_id=None)
    except Exception:
        pass
    now = datetime.now()
    today0 = _day_start(now)
    days = [(_day_start(now - timedelta(days=i))).date().isoformat() for i in range(6, -1, -1)]
    day7 = _day_start(now - timedelta(days=6))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            users_total = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE created_at >= %s", (today0,))
            users_today = int((cur.fetchone() or {}).get("c") or 0)

            cur.execute(
                """
                SELECT
                  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok_n,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS fail_n,
                  SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END) AS partial_n,
                  COUNT(*) AS total_n
                FROM publish_events
                WHERE created_at >= %s
                """,
                (today0,),
            )
            pub = cur.fetchone() or {}
            publish_today = {
                "success": int(pub.get("ok_n") or 0),
                "failed": int(pub.get("fail_n") or 0),
                "partial": int(pub.get("partial_n") or 0),
                "total": int(pub.get("total_n") or 0),
            }

            cur.execute("SELECT COUNT(*) AS c FROM platform_accounts")
            accounts_total = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute(
                "SELECT COUNT(*) AS c FROM platform_accounts WHERE last_valid=0"
            )
            accounts_invalid = int((cur.fetchone() or {}).get("c") or 0)

            cur.execute(
                """
                SELECT DATE(created_at) AS d, COUNT(*) AS c
                FROM users
                WHERE created_at >= %s
                GROUP BY DATE(created_at)
                """,
                (day7,),
            )
            reg_map = {
                (r["d"].isoformat() if hasattr(r["d"], "isoformat") else str(r["d"])): int(r["c"] or 0)
                for r in (cur.fetchall() or [])
            }

            cur.execute(
                """
                SELECT DATE(created_at) AS d,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok_n,
                       SUM(CASE WHEN status IN ('failed','partial') THEN 1 ELSE 0 END) AS bad_n
                FROM publish_events
                WHERE created_at >= %s
                GROUP BY DATE(created_at)
                """,
                (day7,),
            )
            pub_map = {}
            for r in cur.fetchall() or []:
                key = r["d"].isoformat() if hasattr(r["d"], "isoformat") else str(r["d"])
                pub_map[key] = {
                    "success": int(r.get("ok_n") or 0),
                    "failed": int(r.get("bad_n") or 0),
                }

    agents = agent_store.list_agents()
    agents_online = sum(1 for a in agents if a.get("online"))

    trend = []
    for d in days:
        trend.append(
            {
                "date": d,
                "registrations": reg_map.get(d, 0),
                "publish_success": (pub_map.get(d) or {}).get("success", 0),
                "publish_failed": (pub_map.get(d) or {}).get("failed", 0),
            }
        )

    return {
        "ok": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "users_total": users_total,
        "users_today": users_today,
        "publish_today": publish_today,
        "agents_online": agents_online,
        "accounts_total": accounts_total,
        "accounts_invalid": accounts_invalid,
        "trend_7d": trend,
    }


def agents_dashboard(*, job_status: str = "", job_type: str = "", limit: int = 80) -> dict[str, Any]:
    agents = _attach_users(agent_store.list_agents())
    jobs = _attach_users(
        agent_store.list_jobs(status=job_status, job_type=job_type, limit=limit)
    )
    return {
        "ok": True,
        "online_seconds": getattr(agent_store, "AGENT_ONLINE_SECONDS", 120),
        "agents": agents,
        "jobs": jobs,
        "agents_online": sum(1 for a in agents if a.get("online")),
        "agents_total": len(agents),
    }


def upload_tasks(*, status: str = "", limit: int = 100) -> dict[str, Any]:
    items = task_store.list_tasks(status=status, limit=limit)
    return {
        "ok": True,
        "total": len(items),
        "items": items,
        "running": sum(1 for x in items if x.get("status") == "running"),
    }


def _next_run_at(daily_time: str, last_run_date: str, enabled: bool) -> str:
    if not enabled:
        return ""
    hhmm = (daily_time or "").strip()
    if len(hhmm) != 5 or hhmm[2] != ":":
        return ""
    try:
        hour, minute = int(hhmm[:2]), int(hhmm[3:])
    except ValueError:
        return ""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if last_run_date == today or candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


def schedules() -> dict[str, Any]:
    from web_mvp import folder_queue as fq
    from web_mvp import subscriptions as subs

    items: list[dict[str, Any]] = []
    for sub in subs.load_subscriptions():
        if not isinstance(sub, dict):
            continue
        items.append(
            {
                "kind": "subscription",
                "kind_label": "博主订阅",
                "id": sub.get("id") or "",
                "title": sub.get("title") or sub.get("homepage_url") or sub.get("id") or "",
                "enabled": bool(sub.get("enabled")),
                "daily_time": sub.get("daily_time") or "",
                "source_platform": sub.get("source_platform") or "",
                "last_run_at": sub.get("last_run_at") or "",
                "last_run_date": sub.get("last_run_date") or "",
                "last_status": sub.get("last_status") or "",
                "last_error": sub.get("last_error") or "",
                "next_run_at": _next_run_at(
                    str(sub.get("daily_time") or ""),
                    str(sub.get("last_run_date") or ""),
                    bool(sub.get("enabled")),
                ),
            }
        )
    for queue in fq.load_queues():
        if not isinstance(queue, dict):
            continue
        items.append(
            {
                "kind": "folder_queue",
                "kind_label": "文件夹队列",
                "id": queue.get("id") or "",
                "title": queue.get("title") or queue.get("folder_path") or queue.get("id") or "",
                "enabled": bool(queue.get("enabled")),
                "daily_time": queue.get("daily_time") or "",
                "source_platform": "",
                "last_run_at": queue.get("last_run_at") or "",
                "last_run_date": queue.get("last_run_date") or "",
                "last_status": queue.get("last_status") or "",
                "last_error": queue.get("last_error") or "",
                "next_run_at": _next_run_at(
                    str(queue.get("daily_time") or ""),
                    str(queue.get("last_run_date") or ""),
                    bool(queue.get("enabled")),
                ),
            }
        )
    items.sort(key=lambda x: (not x.get("enabled"), x.get("next_run_at") or "9999", x.get("kind") or ""))
    return {"ok": True, "total": len(items), "items": items}
