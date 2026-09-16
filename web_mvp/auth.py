"""AutoSelf auth: email register with verification code + bearer sessions."""

from __future__ import annotations

import random
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor
from werkzeug.security import check_password_hash, generate_password_hash

from conf import (
    AUTH_TOKEN_DAYS,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
)
from web_mvp.mailer import send_verification_code_email

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u4e00-\u9fff]{2,32}$")
CODE_TTL_MINUTES = 10


class AuthError(Exception):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _connect():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=int(MYSQL_PORT),
        user=MYSQL_USER,
        password=MYSQL_PASSWORD or "",
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


@contextmanager
def db_conn() -> Iterator[Any]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _public_user(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "email": row.get("email") or "",
        "username": row["username"],
        "role": row.get("role") or "user",
        "status": row["status"],
        "plan": row.get("plan") or "free",
        "created_at": row["created_at"].isoformat(timespec="seconds")
        if hasattr(row.get("created_at"), "isoformat")
        else str(row.get("created_at") or ""),
    }


def validate_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not value or not _EMAIL_RE.match(value) or len(value) > 255:
        raise AuthError("请输入有效邮箱")
    return value


def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not name:
        return ""
    if not _USERNAME_RE.match(name):
        raise AuthError("昵称需 2～32 位（字母/数字/下划线/中文）")
    return name


def validate_password(password: str) -> str:
    pwd = password or ""
    if len(pwd) < 6 or len(pwd) > 72:
        raise AuthError("密码长度需 6～72 位")
    return pwd


def _generate_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def _create_session(conn, user_id: int) -> tuple[str, datetime]:
    token = secrets.token_hex(32)
    expires_at = datetime.now() + timedelta(days=max(1, int(AUTH_TOKEN_DAYS)))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (user_id, token, expires_at) VALUES (%s, %s, %s)",
            (user_id, token, expires_at),
        )
    return token, expires_at


def _session_payload(conn, row: dict) -> dict:
    token, expires_at = _create_session(conn, int(row["id"]))
    return {
        "ok": True,
        "token": token,
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "user": _public_user(row),
    }


def request_register_code(email: str, password: str, username: str = "") -> dict:
    """Store pending registration and email a 6-digit code (10 min)."""
    mail = validate_email(email)
    pwd = validate_password(password)
    name = validate_username(username) or mail.split("@", 1)[0][:32]
    if len(name) < 2:
        name = f"user{secrets.token_hex(2)}"
    password_hash = generate_password_hash(pwd)
    code = _generate_code()
    expires_at = datetime.now() + timedelta(minutes=CODE_TTL_MINUTES)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email=%s LIMIT 1", (mail,))
            if cur.fetchone():
                raise AuthError("该邮箱已注册，请直接登录")
            cur.execute(
                """
                INSERT INTO pending_users (email, username, password_hash, verification_code, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  username=VALUES(username),
                  password_hash=VALUES(password_hash),
                  verification_code=VALUES(verification_code),
                  expires_at=VALUES(expires_at),
                  updated_at=CURRENT_TIMESTAMP(3)
                """,
                (mail, name, password_hash, code, expires_at),
            )

    try:
        send_verification_code_email(to=mail, code=code, purpose="register")
    except Exception as exc:
        raise AuthError(f"验证码发送失败：{exc}", status=502) from exc

    return {"ok": True, "email": mail, "expires_in_seconds": CODE_TTL_MINUTES * 60}


def verify_register(email: str, code: str) -> dict:
    mail = validate_email(email)
    raw_code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", raw_code):
        raise AuthError("请输入 6 位数字验证码")

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT email, username, password_hash, verification_code, expires_at
                FROM pending_users WHERE email=%s LIMIT 1
                """,
                (mail,),
            )
            pending = cur.fetchone()
            if not pending:
                raise AuthError("请先获取验证码")
            expires_at = pending["expires_at"]
            if isinstance(expires_at, datetime) and expires_at < datetime.now():
                cur.execute("DELETE FROM pending_users WHERE email=%s", (mail,))
                raise AuthError("验证码已过期，请重新获取")
            if str(pending["verification_code"]) != raw_code:
                raise AuthError("验证码错误")

            cur.execute("SELECT id FROM users WHERE email=%s LIMIT 1", (mail,))
            if cur.fetchone():
                cur.execute("DELETE FROM pending_users WHERE email=%s", (mail,))
                raise AuthError("该邮箱已注册，请直接登录")

            try:
                cur.execute(
                    """
                    INSERT INTO users (email, username, password_hash, status, plan, role)
                    VALUES (%s, %s, %s, 'active', 'free', 'user')
                    """,
                    (mail, pending["username"], pending["password_hash"]),
                )
            except pymysql.err.IntegrityError as exc:
                raise AuthError("该邮箱或昵称已存在") from exc
            user_id = cur.lastrowid
            cur.execute("DELETE FROM pending_users WHERE email=%s", (mail,))
            cur.execute(
                "SELECT id, email, username, role, status, plan, created_at FROM users WHERE id=%s",
                (user_id,),
            )
            row = cur.fetchone()
        return _session_payload(conn, row)


def login(email: str, password: str, *, require_role: str | None = "user") -> dict:
    mail = validate_email(email)
    pwd = password or ""
    if not pwd:
        raise AuthError("请输入密码")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, email, username, password_hash, role, status, plan, created_at
                FROM users WHERE email=%s LIMIT 1
                """,
                (mail,),
            )
            row = cur.fetchone()
        if not row or not check_password_hash(row["password_hash"], pwd):
            raise AuthError("邮箱或密码错误", status=401)
        if row["status"] != "active":
            raise AuthError("账号已禁用，请联系管理员", status=403)
        role = (row.get("role") or "user").strip()
        if require_role == "user" and role != "user":
            raise AuthError("请使用后台入口登录管理员账号", status=403)
        if require_role == "admin" and role != "admin":
            raise AuthError("无后台权限", status=403)
        return _session_payload(conn, row)


def admin_login(email: str, password: str) -> dict:
    return login(email, password, require_role="admin")


def logout(token: str | None) -> None:
    raw = (token or "").strip()
    if not raw:
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token=%s", (raw,))


def resolve_token(token: str | None) -> dict:
    raw = (token or "").strip()
    if not raw:
        raise AuthError("未登录", status=401)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.email, u.username, u.role, u.status, u.plan, u.created_at, s.expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token=%s
                LIMIT 1
                """,
                (raw,),
            )
            row = cur.fetchone()
            if not row:
                raise AuthError("登录已失效，请重新登录", status=401)
            expires_at = row["expires_at"]
            if isinstance(expires_at, datetime) and expires_at < datetime.now():
                cur.execute("DELETE FROM sessions WHERE token=%s", (raw,))
                raise AuthError("登录已过期，请重新登录", status=401)
            if row["status"] != "active":
                cur.execute("DELETE FROM sessions WHERE token=%s", (raw,))
                raise AuthError("账号已禁用，请联系管理员", status=403)
    return _public_user(row)


def ensure_role_column() -> None:
    """Idempotent: add users.role if missing."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME='users' AND COLUMN_NAME='role'
                """,
                (MYSQL_DATABASE,),
            )
            row = cur.fetchone()
            if int(row["c"] if isinstance(row, dict) else row[0]) == 0:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'user' AFTER plan"
                )


def ensure_admin_user(
    *,
    email: str = "3360133176@qq.com",
    password: str = "123456",
    username: str = "admin",
) -> None:
    """Create or reset the bootstrap admin account."""
    ensure_role_column()
    mail = validate_email(email)
    pwd_hash = generate_password_hash(password)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email=%s LIMIT 1", (mail,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """
                    UPDATE users
                    SET username=%s, password_hash=%s, role='admin', status='active', plan='free'
                    WHERE email=%s
                    """,
                    (username, pwd_hash, mail),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users (email, username, password_hash, status, plan, role)
                    VALUES (%s, %s, %s, 'active', 'free', 'admin')
                    """,
                    (mail, username, pwd_hash),
                )


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


# Backward-compatible aliases (old username register removed).
def register(email: str, password: str, username: str = "") -> dict:
    """Deprecated path: request code then user must verify. Prefer request_register_code."""
    return request_register_code(email, password, username)
