"""Platform account bindings for admin overview (per-user ownership + last check)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

PLATFORM_LABELS = {
    "douyin": "抖音",
    "kuaishou": "快手",
    "bilibili": "B站",
    "tencent": "微信视频号",
}


def upsert_account(
    *,
    platform: str,
    account_key: str,
    user_id: int | None = None,
    display_name: str = "",
    platform_id: str = "",
    last_valid: bool | None = None,
    last_error: str = "",
) -> None:
    from web_mvp.auth import db_conn

    platform = (platform or "").strip()
    account_key = (account_key or "").strip()
    if not platform or not account_key:
        return
    now = datetime.now()
    checked_at = now if last_valid is not None else None
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_accounts
                  (user_id, platform, account_key, display_name, platform_id,
                   last_valid, last_checked_at, last_error, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  user_id=COALESCE(VALUES(user_id), user_id),
                  display_name=IF(VALUES(display_name)='', display_name, VALUES(display_name)),
                  platform_id=IF(VALUES(platform_id)='', platform_id, VALUES(platform_id)),
                  last_valid=IF(VALUES(last_valid) IS NULL, last_valid, VALUES(last_valid)),
                  last_checked_at=IF(VALUES(last_checked_at) IS NULL, last_checked_at, VALUES(last_checked_at)),
                  last_error=IF(VALUES(last_valid) IS NULL, last_error, VALUES(last_error)),
                  updated_at=VALUES(updated_at)
                """,
                (
                    int(user_id) if user_id else None,
                    platform[:32],
                    account_key[:128],
                    (display_name or "")[:128],
                    (platform_id or "")[:128],
                    None if last_valid is None else (1 if last_valid else 0),
                    checked_at,
                    (last_error or "")[:300],
                    now,
                    now,
                ),
            )


def sync_from_cookie_files(accounts: list[dict], *, user_id: int | None = None) -> None:
    """Upsert accounts discovered from cookies/ + profiles."""
    for a in accounts or []:
        try:
            upsert_account(
                platform=a.get("platform") or "",
                account_key=a.get("account") or "",
                user_id=user_id,
                display_name=a.get("display_name") or "",
                platform_id=a.get("platform_id") or "",
                last_valid=a.get("valid") if "valid" in a else None,
            )
        except Exception as exc:
            print(f"[platform_accounts] upsert failed: {exc}", flush=True)


def admin_list_accounts(
    *,
    q: str = "",
    platform: str = "",
    user_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    from web_mvp.auth import db_conn

    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 50)))
    where = ["1=1"]
    args: list[Any] = []
    text = (q or "").strip()
    if text:
        where.append(
            "(a.display_name LIKE %s OR a.account_key LIKE %s OR u.email LIKE %s OR u.username LIKE %s)"
        )
        like = f"%{text}%"
        args.extend([like, like, like, like])
    if platform:
        where.append("a.platform=%s")
        args.append(platform)
    if user_id:
        where.append("a.user_id=%s")
        args.append(int(user_id))
    clause = " AND ".join(where)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM platform_accounts a
                LEFT JOIN users u ON u.id = a.user_id
                WHERE {clause}
                """,
                args,
            )
            total = int((cur.fetchone() or {}).get("c") or 0)
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT a.id, a.user_id, a.platform, a.account_key, a.display_name, a.platform_id,
                       a.last_valid, a.last_checked_at, a.last_error, a.created_at, a.updated_at,
                       u.email AS user_email, u.username AS user_name
                FROM platform_accounts a
                LEFT JOIN users u ON u.id = a.user_id
                WHERE {clause}
                ORDER BY a.updated_at DESC, a.id DESC
                LIMIT %s OFFSET %s
                """,
                [*args, page_size, offset],
            )
            rows = cur.fetchall() or []

    items = []
    for r in rows:
        valid = r.get("last_valid")
        if valid is None:
            login_status = "未知"
            login_cls = "muted"
        elif int(valid) == 1:
            login_status = "登录正常"
            login_cls = "ok"
        else:
            login_status = "登录已失效"
            login_cls = "bad"
        checked = r.get("last_checked_at")
        updated = r.get("updated_at")
        items.append(
            {
                "id": int(r["id"]),
                "user_id": int(r["user_id"]) if r.get("user_id") else None,
                "user_email": r.get("user_email") or "",
                "user_name": r.get("user_name") or "",
                "platform": r.get("platform") or "",
                "platform_label": PLATFORM_LABELS.get(r.get("platform") or "", r.get("platform") or ""),
                "account_key": r.get("account_key") or "",
                "display_name": r.get("display_name") or r.get("account_key") or "",
                "platform_id": r.get("platform_id") or "",
                "login_status": login_status,
                "login_cls": login_cls,
                "last_error": r.get("last_error") or "",
                "last_checked_at": checked.isoformat(timespec="seconds")
                if hasattr(checked, "isoformat")
                else str(checked or ""),
                "updated_at": updated.isoformat(timespec="seconds")
                if hasattr(updated, "isoformat")
                else str(updated or ""),
            }
        )
    return {"ok": True, "total": total, "page": page, "page_size": page_size, "accounts": items}
