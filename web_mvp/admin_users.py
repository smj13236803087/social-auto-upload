"""Admin user CRUD for AutoSelf backoffice."""

from __future__ import annotations

from typing import Any

import pymysql
from werkzeug.security import generate_password_hash

from web_mvp.auth import AuthError, db_conn, validate_email, validate_password, validate_username


def _row_user(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "email": row.get("email") or "",
        "username": row.get("username") or "",
        "role": row.get("role") or "user",
        "status": row.get("status") or "active",
        "plan": row.get("plan") or "free",
        "created_at": row["created_at"].isoformat(timespec="seconds")
        if hasattr(row.get("created_at"), "isoformat")
        else str(row.get("created_at") or ""),
        "updated_at": row["updated_at"].isoformat(timespec="seconds")
        if hasattr(row.get("updated_at"), "isoformat")
        else str(row.get("updated_at") or ""),
    }


def list_users(
    *,
    q: str = "",
    role: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    where = ["1=1"]
    args: list[Any] = []
    text = (q or "").strip()
    if text:
        where.append("(email LIKE %s OR username LIKE %s)")
        like = f"%{text}%"
        args.extend([like, like])
    if role in ("user", "admin"):
        where.append("role=%s")
        args.append(role)
    if status in ("active", "disabled"):
        where.append("status=%s")
        args.append(status)
    clause = " AND ".join(where)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM users WHERE {clause}", args)
            total = int(cur.fetchone()["c"])
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT id, email, username, role, status, plan, created_at, updated_at
                FROM users
                WHERE {clause}
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                [*args, page_size, offset],
            )
            rows = cur.fetchall() or []
    return {
        "ok": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "users": [_row_user(r) for r in rows],
    }


def create_user(
    *,
    email: str,
    password: str,
    username: str = "",
    role: str = "user",
    status: str = "active",
    plan: str = "free",
) -> dict:
    mail = validate_email(email)
    pwd = validate_password(password)
    name = validate_username(username) or mail.split("@", 1)[0][:32]
    if role not in ("user", "admin"):
        raise AuthError("role 仅支持 user/admin")
    if status not in ("active", "disabled"):
        raise AuthError("status 仅支持 active/disabled")
    plan = (plan or "free").strip()[:32] or "free"
    with db_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO users (email, username, password_hash, status, plan, role)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (mail, name, generate_password_hash(pwd), status, plan, role),
                )
            except pymysql.err.IntegrityError as exc:
                raise AuthError("该邮箱已存在") from exc
            user_id = cur.lastrowid
            cur.execute(
                """
                SELECT id, email, username, role, status, plan, created_at, updated_at
                FROM users WHERE id=%s
                """,
                (user_id,),
            )
            row = cur.fetchone()
    return _row_user(row)


def update_user(
    user_id: int,
    *,
    email: str | None = None,
    username: str | None = None,
    password: str | None = None,
    role: str | None = None,
    status: str | None = None,
    plan: str | None = None,
) -> dict:
    fields: list[str] = []
    args: list[Any] = []
    if email is not None:
        fields.append("email=%s")
        args.append(validate_email(email))
    if username is not None:
        name = validate_username(username)
        if not name:
            raise AuthError("昵称不能为空")
        fields.append("username=%s")
        args.append(name)
    if password is not None and str(password).strip():
        fields.append("password_hash=%s")
        args.append(generate_password_hash(validate_password(password)))
    if role is not None:
        if role not in ("user", "admin"):
            raise AuthError("role 仅支持 user/admin")
        fields.append("role=%s")
        args.append(role)
    if status is not None:
        if status not in ("active", "disabled"):
            raise AuthError("status 仅支持 active/disabled")
        fields.append("status=%s")
        args.append(status)
    if plan is not None:
        fields.append("plan=%s")
        args.append((plan or "free").strip()[:32] or "free")
    if not fields:
        raise AuthError("没有可更新的字段")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id=%s", (user_id,))
            if not cur.fetchone():
                raise AuthError("用户不存在", status=404)
            try:
                cur.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE id=%s",
                    [*args, user_id],
                )
            except pymysql.err.IntegrityError as exc:
                raise AuthError("邮箱冲突") from exc
            cur.execute(
                """
                SELECT id, email, username, role, status, plan, created_at, updated_at
                FROM users WHERE id=%s
                """,
                (user_id,),
            )
            row = cur.fetchone()
    return _row_user(row)


def delete_user(user_id: int, *, actor_id: int) -> None:
    if int(user_id) == int(actor_id):
        raise AuthError("不能删除当前登录账号")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
            if cur.rowcount <= 0:
                raise AuthError("用户不存在", status=404)
