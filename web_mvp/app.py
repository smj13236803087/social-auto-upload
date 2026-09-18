"""CN-only Web MVP: bind accounts + upload, paste share URL + download."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, has_request_context, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

from conf import BASE_DIR
from downloader.cn_share import download_share_url
from uploader.bilibili_uploader.runtime import run_biliup_command
from uploader.douyin_uploader.main import (
    DouYinVideo,
    cookie_auth as douyin_cookie_auth,
    douyin_setup,
)
from uploader.ks_uploader.main import (
    KSVideo,
    cookie_auth as kuaishou_cookie_auth,
    ks_setup,
)
from uploader.tencent_uploader.main import (
    TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
    TENCENT_PUBLISH_STRATEGY_SCHEDULED,
    TencentVideo,
    cookie_auth as tencent_cookie_auth,
    tencent_setup,
)
from web_mvp import agent_jobs as agent_store
from web_mvp import admin_ops
from web_mvp import admin_users as admin_user_store
from web_mvp import folder_queue as folder_store
from web_mvp import platform_accounts as account_store
from web_mvp import publish_history as history_store
from web_mvp import subscriptions as sub_store
from web_mvp import task_progress as task_store
# Douyin must run on user machine; cloud web dispatches jobs to local agent.
AGENT_PLATFORMS = frozenset({"douyin"})
from web_mvp.account_profile import resolve_account_profile
from web_mvp.auth import AuthError, extract_bearer, login as auth_login
from web_mvp.auth import admin_login as auth_admin_login
from web_mvp.auth import ensure_admin_user, logout as auth_logout
from web_mvp.auth import resolve_token
from web_mvp.bili_partitions import DEFAULT_BILIBILI_TID, list_bilibili_partitions

PLATFORMS = ("douyin", "kuaishou", "bilibili", "tencent")
PLATFORM_LABELS = {"douyin": "抖音", "kuaishou": "快手", "bilibili": "B站", "tencent": "微信视频号"}
SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"
DEFAULT_BILIBILI_UPLOAD_LINE = "bda2"
AUTH_PUBLIC_PREFIXES = (
    "/static/",
    "/api/health",
    "/api/client-version",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/password",
    "/api/admin/login",
)
AUTH_PUBLIC_EXACT = {"/", "/favicon.ico", "/admin", "/admin/"}

STATIC_DIR = Path(__file__).resolve().parent / "static"
MEDIA_DIR = Path(BASE_DIR) / "tmp" / "media"
DOWNLOADS_DIR = Path(BASE_DIR) / "tmp" / "downloads"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

_DOWNLOAD_INDEX: dict[str, dict] = {}

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
CORS(app)

try:
    ensure_admin_user(email="3360133176@qq.com", password="123456", username="admin")
except Exception as exc:  # noqa: BLE001 - boot without killing server if DB down
    print(f"[autoself] ensure_admin_user skipped: {exc}", flush=True)


def _is_public_path(path: str) -> bool:
    if path in AUTH_PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in AUTH_PUBLIC_PREFIXES)


@app.before_request
def require_auth():
    path = request.path or "/"
    if request.method == "OPTIONS" or _is_public_path(path):
        return None
    if not path.startswith("/api/"):
        return None
    token = extract_bearer(request.headers.get("Authorization"))
    if not token:
        token = (request.args.get("token") or "").strip() or None
    try:
        user = resolve_token(token)
        request.autoself_user = user  # type: ignore[attr-defined]
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": f"鉴权服务异常：{exc}"}), 503

    if path.startswith("/api/admin/") and path != "/api/admin/login":
        if (user.get("role") or "") != "admin":
            return jsonify({"ok": False, "error": "无后台权限"}), 403
    return None


@app.post("/api/auth/register/request-code")
def api_auth_request_code():
    data = request.get_json(force=True, silent=True) or {}
    try:
        from web_mvp.auth import request_register_code

        return jsonify(
            request_register_code(
                data.get("email") or "",
                data.get("password") or "",
                data.get("username") or "",
            )
        )
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": f"发送验证码失败：{exc}"}), 500


@app.post("/api/auth/register/verify")
def api_auth_verify():
    data = request.get_json(force=True, silent=True) or {}
    try:
        from web_mvp.auth import verify_register

        return jsonify(
            verify_register(data.get("email") or "", data.get("code") or "")
        )
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": f"注册失败：{exc}"}), 500


@app.post("/api/auth/register")
def api_auth_register():
    """Backward compatible: treat as request-code (does not finish registration)."""
    return api_auth_request_code()


@app.post("/api/auth/login")
def api_auth_login():
    data = request.get_json(force=True, silent=True) or {}
    try:
        email = data.get("email") or data.get("username") or ""
        return jsonify(auth_login(email, data.get("password") or "", require_role="user"))
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": f"登录失败：{exc}"}), 500


@app.post("/api/auth/password/request-code")
def api_auth_password_request_code():
    data = request.get_json(force=True, silent=True) or {}
    try:
        from web_mvp.auth import request_password_reset_code

        return jsonify(request_password_reset_code(data.get("email") or ""))
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": f"发送验证码失败：{exc}"}), 500


@app.post("/api/auth/password/reset")
def api_auth_password_reset():
    data = request.get_json(force=True, silent=True) or {}
    try:
        from web_mvp.auth import reset_password

        return jsonify(
            reset_password(
                data.get("email") or "",
                data.get("code") or "",
                data.get("password") or data.get("new_password") or "",
            )
        )
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": f"重置密码失败：{exc}"}), 500


@app.post("/api/auth/logout")
def api_auth_logout():
    token = extract_bearer(request.headers.get("Authorization"))
    auth_logout(token)
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_auth_me():
    user = getattr(request, "autoself_user", None)
    if not user:
        return jsonify({"ok": False, "error": "未登录"}), 401
    return jsonify({"ok": True, "user": user})


@app.post("/api/admin/login")
def api_admin_login():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(
            auth_admin_login(data.get("email") or "", data.get("password") or "")
        )
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": f"登录失败：{exc}"}), 500


@app.post("/api/admin/logout")
def api_admin_logout():
    token = extract_bearer(request.headers.get("Authorization"))
    auth_logout(token)
    return jsonify({"ok": True})


@app.get("/api/admin/me")
def api_admin_me():
    user = getattr(request, "autoself_user", None)
    if not user:
        return jsonify({"ok": False, "error": "未登录"}), 401
    return jsonify({"ok": True, "user": user})


@app.get("/api/admin/users")
def api_admin_list_users():
    try:
        return jsonify(
            admin_user_store.list_users(
                q=request.args.get("q") or "",
                role=request.args.get("role") or "",
                status=request.args.get("status") or "",
                page=int(request.args.get("page") or 1),
                page_size=int(request.args.get("page_size") or 20),
            )
        )
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/users")
def api_admin_create_user():
    data = request.get_json(force=True, silent=True) or {}
    try:
        user = admin_user_store.create_user(
            email=data.get("email") or "",
            password=data.get("password") or "",
            username=data.get("username") or "",
            role=data.get("role") or "user",
            status=data.get("status") or "active",
            plan=data.get("plan") or "free",
        )
        return jsonify({"ok": True, "user": user})
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.patch("/api/admin/users/<int:user_id>")
def api_admin_update_user(user_id: int):
    data = request.get_json(force=True, silent=True) or {}
    try:
        user = admin_user_store.update_user(
            user_id,
            email=data.get("email"),
            username=data.get("username"),
            password=data.get("password"),
            role=data.get("role"),
            status=data.get("status"),
            plan=data.get("plan"),
        )
        return jsonify({"ok": True, "user": user})
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.delete("/api/admin/users/<int:user_id>")
def api_admin_delete_user(user_id: int):
    actor = getattr(request, "autoself_user", None) or {}
    try:
        admin_user_store.delete_user(user_id, actor_id=int(actor.get("id") or 0))
        return jsonify({"ok": True})
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/overview")
def api_admin_overview():
    try:
        return jsonify(admin_ops.overview())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/publish-events")
def api_admin_publish_events():
    try:
        user_id_raw = (request.args.get("user_id") or "").strip()
        user_id = int(user_id_raw) if user_id_raw.isdigit() else None
        data = history_store.admin_list_events(
            q=request.args.get("q") or "",
            status=request.args.get("status") or "",
            user_id=user_id,
            page=int(request.args.get("page") or 1),
            page_size=int(request.args.get("page_size") or 20),
        )
        return jsonify(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/platform-accounts")
def api_admin_platform_accounts():
    try:
        try:
            account_store.sync_from_cookie_files(_list_accounts(refresh_profile=False), user_id=None)
        except Exception:
            pass
        user_id_raw = (request.args.get("user_id") or "").strip()
        user_id = int(user_id_raw) if user_id_raw.isdigit() else None
        data = account_store.admin_list_accounts(
            q=request.args.get("q") or "",
            platform=request.args.get("platform") or "",
            user_id=user_id,
            page=int(request.args.get("page") or 1),
            page_size=int(request.args.get("page_size") or 50),
        )
        return jsonify(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/agents")
def api_admin_agents():
    try:
        return jsonify(
            admin_ops.agents_dashboard(
                job_status=request.args.get("status") or "",
                job_type=request.args.get("type") or "",
                limit=int(request.args.get("limit") or 80),
            )
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/agent-jobs/<job_id>/cancel")
def api_admin_agent_job_cancel(job_id: str):
    try:
        from web_mvp import agent_jobs as agent_store

        job = agent_store.cancel_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({"ok": True, "job": job})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/upload-tasks")
def api_admin_upload_tasks():
    try:
        return jsonify(
            admin_ops.upload_tasks(
                status=request.args.get("status") or "",
                limit=int(request.args.get("limit") or 100),
            )
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/schedules")
def api_admin_schedules():
    try:
        return jsonify(admin_ops.schedules())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/sessions")
def api_admin_sessions():
    try:
        from web_mvp.auth import list_sessions

        user_id_raw = (request.args.get("user_id") or "").strip()
        user_id = int(user_id_raw) if user_id_raw.isdigit() else None
        return jsonify(
            list_sessions(
                q=request.args.get("q") or "",
                user_id=user_id,
                page=int(request.args.get("page") or 1),
                page_size=int(request.args.get("page_size") or 50),
            )
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.delete("/api/admin/sessions/<int:session_id>")
def api_admin_session_revoke(session_id: int):
    try:
        from web_mvp.auth import revoke_session

        ok = revoke_session(session_id)
        if not ok:
            return jsonify({"ok": False, "error": "会话不存在或已失效"}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.delete("/api/admin/users/<int:user_id>/sessions")
def api_admin_user_sessions_revoke(user_id: int):
    try:
        from web_mvp.auth import revoke_user_sessions

        n = revoke_user_sessions(user_id)
        return jsonify({"ok": True, "revoked": n})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _run_async(coro):
    return asyncio.run(coro)


def resolve_account_file(platform: str, account_name: str) -> Path:
    account_file = Path(BASE_DIR) / "cookies" / f"{platform}_{account_name}.json"
    account_file.parent.mkdir(exist_ok=True)
    return account_file


def _is_local_client() -> bool:
    """True when the page/API is used from desktop (localhost), not the public cloud site."""
    host = (request.host or "").split(":")[0].strip().lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    # behind local reverse proxy
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    remote = (request.remote_addr or "").strip()
    return remote in {"127.0.0.1", "::1"} or xff in {"127.0.0.1", "::1"}


def _current_user() -> dict:

    return getattr(request, "autoself_user", None) or {}


def _try_current_user_id() -> int | None:
    if not has_request_context():
        return None
    uid = int(_current_user().get("id") or 0)
    return uid or None


def _public_server_base() -> str:
    env = (os.environ.get("AUTOSELF_PUBLIC_URL") or "").strip().rstrip("/")
    if env:
        return env
    # Prefer public domain; request.url_root may be raw IP behind nginx.
    return "https://autopost.com.cn"


def _agent_server_base() -> str:
    env = (os.environ.get("AUTOSELF_AGENT_URL") or "").strip().rstrip("/")
    if env:
        return env
    # Some client networks reset HTTPS to this host; HTTP works for agent API.
    return "http://autopost.com.cn"


def _save_cookie_blob(platform: str, account: str, cookie: dict) -> Path:
    path = resolve_account_file(platform, account)
    path.write_text(json.dumps(cookie, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _wait_agent_job(job_id: str, *, timeout_sec: float = 900) -> dict:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        job = agent_store.get_job(job_id)
        if not job:
            raise RuntimeError("本机助手任务不存在")
        status = job.get("status")
        if status == "done":
            return job
        if status in {"error", "cancelled"}:
            raise RuntimeError(job.get("error") or "本机助手任务失败")
        time.sleep(1.0)
    agent_store.fail_job(job_id, "等待本机助手超时")
    raise RuntimeError("等待本机助手超时，请确认本机助手仍在运行")


def _login_via_agent_sse(user_id: int, platform: str, account: str):
    plat_label = PLATFORM_LABELS.get(platform, platform)
    # 连点扫码时复用进行中的登录任务，避免取消旧任务后网页等新任务、助手仍卡在旧任务。
    existing = agent_store.find_active_job(user_id=user_id, platform=platform, job_type="login")
    if existing:
        job = existing
        job_id = job["id"]
    else:
        job = agent_store.create_job(
            user_id=user_id,
            job_type="login",
            platform=platform,
            account=account,
        )
        job_id = job["id"]

    def stream():
        last_qr = None
        last_event_n = 0
        yield (
            "data: "
            + json.dumps(
                {
                    "event": "status",
                    "message": f"已派发到本机助手，正在出码…请稍候，出现二维码后再用{plat_label} App 扫",
                    "via": "agent",
                    "job_id": job_id,
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )
        deadline = time.time() + 600
        while time.time() < deadline:
            cur = agent_store.get_job(job_id)
            if not cur:
                yield (
                    "data: "
                    + json.dumps({"event": "error", "message": "本机助手任务丢失", "via": "agent"}, ensure_ascii=False)
                    + "\n\n"
                )
                return
            qr = cur.get("qrcode")
            if qr and qr != last_qr:
                last_qr = qr
                data = dict(qr)
                hint = (data.get("hint") or "").strip()
                if not hint:
                    data["hint"] = f"请使用{plat_label} App 扫描二维码登录（本机助手）"
                elif plat_label not in hint:
                    data["hint"] = f"{plat_label} · {hint}"
                data["platform"] = platform
                yield (
                    "data: "
                    + json.dumps({"event": "qrcode", "qrcode": data, "via": "agent"}, ensure_ascii=False)
                    + "\n\n"
                )
            events = cur.get("events") or []
            while last_event_n < len(events):
                ev = dict(events[last_event_n] or {})
                last_event_n += 1
                ev.setdefault("platform", platform)
                ev.setdefault("via", "agent")
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
            status = cur.get("status")
            if status == "done":
                result = cur.get("result") or {"success": True, "message": "登录成功"}
                yield (
                    "data: "
                    + json.dumps({"event": "done", "result": result, "via": "agent"}, ensure_ascii=False)
                    + "\n\n"
                )
                return
            if status in {"error", "cancelled"}:
                yield (
                    "data: "
                    + json.dumps(
                        {"event": "error", "message": cur.get("error") or "本机助手登录失败", "via": "agent"},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                return
            time.sleep(0.8)
        agent_store.fail_job(job_id, "等待本机助手扫码超时")
        yield (
            "data: "
            + json.dumps({"event": "error", "message": "等待本机助手超时，请确认助手仍在运行", "via": "agent"}, ensure_ascii=False)
            + "\n\n"
        )

    return Response(stream(), mimetype="text/event-stream")


def _publish_via_agent(
    *,
    user_id: int,
    platform: str,
    account: str,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    schedule: datetime | int,
) -> None:
    account_file = resolve_account_file(platform, account)
    if not account_file.exists():
        raise RuntimeError(f"{PLATFORM_LABELS.get(platform, platform)} Cookie 不存在，请先扫码登录账号 {account}")
    cookie = json.loads(account_file.read_text(encoding="utf-8"))
    if not isinstance(cookie, dict):
        raise RuntimeError("账号 Cookie 文件格式无效")
    schedule_payload: str | int
    if isinstance(schedule, datetime):
        schedule_payload = schedule.strftime(SCHEDULE_FORMAT)
    else:
        schedule_payload = 0
    # 发布优先：取消排队中的扫码登录，避免助手卡在别人账号的二维码上导致网页一直转圈
    agent_store.cancel_pending_jobs(user_id=user_id, job_type="login")
    job = agent_store.create_job(
        user_id=user_id,
        job_type="publish",
        platform=platform,
        account=account,
        payload={
            "title": title,
            "description": description or title,
            "tags": tags,
            "schedule": schedule_payload,
            "cookie": cookie,
            "media_path": str(video_path.resolve()),
            "filename": video_path.name,
        },
    )
    finished = _wait_agent_job(job["id"], timeout_sec=900)
    result = finished.get("result") or {}
    if result.get("success") is False:
        raise RuntimeError(result.get("message") or finished.get("error") or "本机助手发布失败")


def parse_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []
    # Accept comma / whitespace / full-width comma separated tags; strip leading #.
    parts = re.split(r"[,，\s]+", str(raw_tags).strip())
    tags: list[str] = []
    for item in parts:
        cleaned = item.strip().lstrip("#").strip()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags


def parse_schedule(raw_schedule: str | None) -> datetime | int:
    if not raw_schedule:
        return 0
    return datetime.strptime(raw_schedule, SCHEDULE_FORMAT)


def _list_accounts(*, refresh_profile: bool = False) -> list[dict]:
    cookies_dir = Path(BASE_DIR) / "cookies"
    cookies_dir.mkdir(exist_ok=True)
    items: list[dict] = []
    for platform in PLATFORMS:
        for path in sorted(cookies_dir.glob(f"{platform}_*.json")):
            account = path.stem[len(platform) + 1 :]
            if not account:
                continue
            profile = resolve_account_profile(
                platform,
                account,
                path,
                force=refresh_profile,
            )
            display_name = profile.get("display_name") or account
            platform_id = profile.get("platform_id") or ""
            items.append(
                {
                    "platform": platform,
                    "label": PLATFORM_LABELS[platform],
                    "account": account,  # local cookie key, used by APIs
                    "local_name": account,
                    "display_name": display_name,
                    "platform_id": platform_id,
                    "profile_source": profile.get("source") or "",
                    "path": str(path),
                }
            )
    return items


async def _check_douyin(account: str) -> bool:
    account_file = resolve_account_file("douyin", account)
    if not account_file.exists():
        return False
    return await douyin_cookie_auth(str(account_file))


async def _check_kuaishou(account: str) -> bool:
    account_file = resolve_account_file("kuaishou", account)
    if not account_file.exists():
        return False
    return await kuaishou_cookie_auth(str(account_file))


def _check_bilibili(account: str) -> bool:
    account_file = resolve_account_file("bilibili", account)
    if not account_file.exists():
        return False
    # Prefer nav probe first — cookie-only QR login may lack oauth tokens,
    # and biliup renew is slow / can block the accounts list refresh.
    try:
        from web_mvp.account_profile import _fetch_bilibili

        _fetch_bilibili(account_file)
        return True
    except Exception:
        pass
    result = run_biliup_command(["-u", str(account_file), "renew"])
    return result.returncode == 0


async def _check_tencent(account: str) -> bool:
    account_file = resolve_account_file("tencent", account)
    if not account_file.exists():
        return False
    return await tencent_cookie_auth(str(account_file))


def _check_account(platform: str, account: str) -> bool:
    if platform == "douyin":
        return _run_async(_check_douyin(account))
    if platform == "kuaishou":
        return _run_async(_check_kuaishou(account))
    if platform == "bilibili":
        return _check_bilibili(account)
    if platform == "tencent":
        return _run_async(_check_tencent(account))
    raise ValueError(f"不支持的平台: {platform}")


def _web_client_version() -> str:
    """Fingerprint of the main web UI so open clients can soft-reload after deploy."""
    index = STATIC_DIR / "index.html"
    try:
        st = index.stat()
        return f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        return "0"


@app.get("/")
def index():
    resp = send_from_directory(STATIC_DIR, "index.html")
    # Always revalidate so desktop shell / long-lived tabs get new HTML after deploy.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/admin")
@app.get("/admin/")
def admin_index():
    resp = send_from_directory(STATIC_DIR, "admin.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/client-version")
def client_version():
    return jsonify(
        {
            "ok": True,
            "web": _web_client_version(),
            # Native desktop shell version is independent; bump when shipping new .dmg/.pkg
            "desktop_min": os.environ.get("AUTOSELF_DESKTOP_MIN_VERSION", "1.0.0"),
        }
    )


@app.get("/api/bilibili-partitions")
def bilibili_partitions():
    return jsonify(
        {
            "ok": True,
            "default_tid": DEFAULT_BILIBILI_TID,
            "groups": list_bilibili_partitions(),
        }
    )


def _pick_folder_native() -> str:
    """Open a native folder dialog on the machine running this server."""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "选择视频文件夹")'
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "User canceled" in err or proc.returncode == 1:
                raise RuntimeError("已取消选择")
            raise RuntimeError(err or "打开文件夹选择器失败")
        path = (proc.stdout or "").strip().rstrip("/")
        if not path:
            raise RuntimeError("已取消选择")
        return path

    if sys.platform.startswith("win"):
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = '选择视频文件夹'; "
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=300,
        )
        path = (proc.stdout or "").strip()
        if not path:
            raise RuntimeError("已取消选择或打开文件夹选择器失败")
        return path

    for cmd in (
        ["zenity", "--file-selection", "--directory", "--title=选择视频文件夹"],
        ["kdialog", "--getexistingdirectory", str(Path.home()), "选择视频文件夹"],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            path = (proc.stdout or "").strip()
            if proc.returncode == 0 and path:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="选择视频文件夹")
        root.destroy()
        if not path:
            raise RuntimeError("已取消选择")
        return path
    except Exception as exc:
        raise RuntimeError(f"无法打开文件夹选择器: {exc}") from exc


@app.post("/api/pick-folder")
def pick_folder():
    try:
        path = _pick_folder_native()
        return jsonify({"ok": True, "path": path})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "platforms": list(PLATFORMS),
            "web": _web_client_version(),
        }
    )


@app.get("/api/accounts")
def list_accounts():
    with_check = request.args.get("check", "0") in ("1", "true", "yes")
    refresh_profile = with_check or request.args.get("refresh_profile", "0") in ("1", "true", "yes")
    accounts = _list_accounts(refresh_profile=refresh_profile)
    if with_check:
        for item in accounts:
            try:
                item["valid"] = _check_account(item["platform"], item["account"])
            except Exception as exc:
                item["valid"] = False
                item["error"] = str(exc)
    uid = _try_current_user_id()
    try:
        account_store.sync_from_cookie_files(accounts, user_id=uid)
    except Exception:
        pass
    return jsonify({"accounts": accounts})


@app.get("/api/accounts/check")
def check_account():
    platform = (request.args.get("platform") or "").strip()
    account = (request.args.get("account") or "").strip()
    if platform not in PLATFORMS or not account:
        return jsonify({"ok": False, "error": "platform / account 必填"}), 400
    try:
        valid = _check_account(platform, account)
        try:
            account_store.upsert_account(
                platform=platform,
                account_key=account,
                user_id=_try_current_user_id(),
                last_valid=bool(valid),
                last_error="" if valid else "登录已失效",
            )
        except Exception:
            pass
        return jsonify({"ok": True, "platform": platform, "account": account, "valid": valid})
    except Exception as exc:
        try:
            account_store.upsert_account(
                platform=platform,
                account_key=account,
                user_id=_try_current_user_id(),
                last_valid=False,
                last_error=str(exc)[:300],
            )
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.delete("/api/accounts")
def delete_account():
    platform = (request.args.get("platform") or "").strip()
    account = (request.args.get("account") or "").strip()
    if platform not in PLATFORMS or not account:
        return jsonify({"ok": False, "error": "platform / account 必填"}), 400
    path = resolve_account_file(platform, account)
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


_login_gate = threading.Lock()
_login_busy_label = ""
_login_cancel = threading.Event()
_login_session_id = 0


def _login_is_cancelled(session_id: int) -> bool:
    return _login_cancel.is_set() or session_id != _login_session_id


@app.get("/api/accounts/login")
def login_account_sse():
    """SSE login for douyin/kuaishou/bilibili/tencent (QR in browser)."""
    platform = (request.args.get("platform") or "").strip()
    account = (request.args.get("account") or "").strip()
    headed = (request.args.get("headed") or "0") in ("1", "true", "yes")
    force_cloud = (request.args.get("cloud") or "0") in ("1", "true", "yes")
    if platform not in PLATFORMS or not account:
        return jsonify({"ok": False, "error": "platform / account 必填"}), 400

    user_id = _try_current_user_id()
    # 公网抖音：优先派给在线本机助手；助手未开则提示去启动（不再开一套本地网站）
    if (
        not force_cloud
        and not _is_local_client()
        and platform in AGENT_PLATFORMS
    ):
        if user_id and agent_store.is_agent_online(user_id):
            return _login_via_agent_sse(user_id, platform, account)
        return jsonify(
            {
                "ok": False,
                "error": "抖音需要本机助手（登录仍用当前网页账号）",
                "code": "douyin_need_agent",
                "hint": "下载并双击「本机助手」，保持运行后，再点扫码登录",
            }
        ), 503

    if (
        not force_cloud
        and user_id
        and platform in AGENT_PLATFORMS
        and agent_store.is_agent_online(user_id)
    ):
        return _login_via_agent_sse(user_id, platform, account)

    plat_label = PLATFORM_LABELS.get(platform, platform)
    global _login_busy_label, _login_session_id

    # 再次点扫码：取消上一路，等它释放闸门后再开新的（2G 机器仍只跑一路浏览器）
    prev_label = _login_busy_label or "其他平台"
    _login_cancel.set()
    acquired = _login_gate.acquire(timeout=25)
    if not acquired:
        # 上一路卡住时强制夺锁，避免永远进不去
        try:
            if _login_gate.locked():
                _login_gate.release()
        except RuntimeError:
            pass
        _login_gate.acquire()

    _login_cancel.clear()
    _login_session_id += 1
    session_id = _login_session_id
    _login_busy_label = plat_label

    events: queue.Queue = queue.Queue()

    def qrcode_callback(payload: dict):
        if _login_is_cancelled(session_id):
            return
        data = dict(payload or {})
        hint = (data.get("hint") or "").strip()
        if not hint:
            data["hint"] = f"请使用{plat_label} App 扫描二维码登录"
        elif plat_label not in hint:
            data["hint"] = f"{plat_label} · {hint}"
        data["platform"] = platform
        events.put({"event": "qrcode", "qrcode": data})

    def event_callback(payload: dict):
        if _login_is_cancelled(session_id):
            return
        data = dict(payload or {})
        data.setdefault("platform", platform)
        events.put(data)

    def cancel_check() -> bool:
        return _login_is_cancelled(session_id)

    def worker():
        try:
            if cancel_check():
                events.put({"event": "error", "message": "登录已取消"})
                return
            account_file = str(resolve_account_file(platform, account))
            headless = not headed
            if platform == "douyin":
                result = _run_async(
                    douyin_setup(
                        account_file,
                        handle=True,
                        return_detail=True,
                        qrcode_callback=qrcode_callback,
                        event_callback=event_callback,
                        cancel_check=cancel_check,
                        headless=headless,
                    )
                )
            elif platform == "kuaishou":
                result = _run_async(
                    ks_setup(
                        account_file,
                        handle=True,
                        return_detail=True,
                        qrcode_callback=qrcode_callback,
                        cancel_check=cancel_check,
                        headless=headless,
                    )
                )
            elif platform == "bilibili":
                import importlib

                from uploader.bilibili_uploader import qr_login as bili_qr_login

                importlib.reload(bili_qr_login)
                result = bili_qr_login.bilibili_qr_login(
                    account_file,
                    qrcode_callback=qrcode_callback,
                    cancel_check=cancel_check,
                )
            elif platform == "tencent":
                result = _run_async(
                    tencent_setup(
                        account_file,
                        handle=True,
                        return_detail=True,
                        qrcode_callback=qrcode_callback,
                        cancel_check=cancel_check,
                        headless=True,
                    )
                )
            else:
                raise ValueError(f"不支持的平台: {platform}")
            if cancel_check():
                events.put({"event": "error", "message": f"已取消上一轮「{prev_label}」登录，开始新的扫码"})
                return
            events.put({"event": "done", "result": result})
        except Exception as exc:
            if cancel_check():
                events.put({"event": "error", "message": "登录已取消"})
            else:
                events.put({"event": "error", "message": str(exc)})

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        global _login_busy_label
        try:
            while True:
                try:
                    item = events.get(timeout=1)
                except queue.Empty:
                    if cancel_check():
                        yield f"data: {json.dumps({'event': 'error', 'message': '登录已被新的扫码替换'}, ensure_ascii=False)}\n\n"
                        break
                    continue
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("event") in ("done", "error"):
                    break
        finally:
            if session_id == _login_session_id:
                _login_busy_label = ""
            try:
                _login_gate.release()
            except RuntimeError:
                pass

    return Response(stream(), mimetype="text/event-stream")



@app.post("/api/accounts/cookie")
def upload_account_cookie():
    """Upload a Playwright storage_state / cookie JSON for a platform account."""
    platform = (request.form.get("platform") or request.args.get("platform") or "").strip()
    account = (request.form.get("account") or request.args.get("account") or "").strip()
    if platform not in PLATFORMS or not account:
        return jsonify({"ok": False, "error": "platform / account 必填"}), 400
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "请选择 Cookie JSON 文件"}), 400
    raw = request.files["file"].read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"ok": False, "error": "文件不是合法 JSON"}), 400
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
        return jsonify({"ok": False, "error": "需要 Playwright storage_state 格式（含 cookies 数组）"}), 400
    names = {str(c.get("name") or "") for c in data.get("cookies") or []}
    if platform == "douyin" and not (names & {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}):
        return jsonify({"ok": False, "error": "抖音 Cookie 缺少 sessionid，请重新在本机导出后再上传"}), 400
    path = resolve_account_file(platform, account)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # light validate for douyin
    valid = None
    if platform == "douyin":
        try:
            valid = bool(_run_async(douyin_cookie_auth(str(path))))
        except Exception as exc:
            return jsonify({"ok": False, "error": f"已保存但校验失败：{exc}", "path": str(path)}), 400
        if not valid:
            return jsonify({"ok": False, "error": "Cookie 已保存但已失效，请重新在本机登录导出", "path": str(path)}), 400
    return jsonify({"ok": True, "path": str(path), "valid": valid if valid is not None else True})


@app.post("/api/accounts/verify-code")
def submit_login_verify_code():
    """Submit SMS / secondary verify code for an in-progress platform login."""
    data = request.get_json(force=True, silent=True) or {}
    platform = (data.get("platform") or request.args.get("platform") or "").strip()
    account = (data.get("account") or request.args.get("account") or "").strip()
    code = str(data.get("code") or "").strip()
    if platform not in PLATFORMS or not account:
        return jsonify({"ok": False, "error": "platform / account 必填"}), 400
    if not code:
        return jsonify({"ok": False, "error": "验证码不能为空"}), 400
    account_file = resolve_account_file(platform, account)
    code_path = account_file.with_name(f"{account_file.stem}_verify_code.txt")
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_path.write_text(code, encoding="utf-8")
    # 本机助手登录时，同步写入任务，供助手拉到本地填入
    user_id = _try_current_user_id()
    via_agent = False
    if user_id:
        job = agent_store.find_active_job(
            user_id=user_id,
            platform=platform,
            account=account,
            job_type="login",
        )
        if job:
            agent_store.set_job_verify_code(job["id"], code)
            via_agent = True
    return jsonify({"ok": True, "via_agent": via_agent})


@app.get("/api/agent/status")
@app.post("/api/agent/status")
def api_agent_status():
    user_id = _try_current_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "未登录"}), 401
    status = agent_store.agent_status(user_id)
    status["ok"] = True
    return jsonify(status)


@app.get("/api/agent/bootstrap")
def api_agent_bootstrap():
    """One-command local agent start helper for the current user."""
    user_id = _try_current_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "未登录"}), 401
    token = extract_bearer(request.headers.get("Authorization")) or (request.args.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "缺少 token"}), 400
    server = _agent_server_base()
    command = (
        "mkdir -p ~/.autoself && "
        f"printf '%s' '{token}' > ~/.autoself/token && "
        f"python -m web_mvp.local_agent --server {server}"
    )
    return jsonify(
        {
            "ok": True,
            "server": server,
            "token": token,
            "command": command,
            "hint": "下载本机助手包后双击即可，无需再登录另一套系统。",
            "package_mac": "/api/agent/package/mac",
            "package_win": "/api/agent/package/win",
        }
    )


@app.get("/api/agent/package/<os_name>")
def api_agent_package(os_name: str):
    """Download a per-user agent zip (token baked in). Double-click to connect to cloud."""
    import io
    import zipfile

    user_id = _try_current_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "未登录"}), 401
    token = extract_bearer(request.headers.get("Authorization")) or (request.args.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "缺少 token"}), 400
    server = _agent_server_base()
    os_name = (os_name or "").strip().lower()
    if os_name not in {"mac", "win", "windows"}:
        return jsonify({"ok": False, "error": "os 仅支持 mac/win"}), 400
    if os_name == "windows":
        os_name = "win"

    buf = io.BytesIO()
    static_desktop = STATIC_DIR / "desktop"
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # connection files
        zf.writestr(
            "autoself-agent/config.json",
            json.dumps({"server": server, "token": token}, ensure_ascii=False, indent=2),
        )
        zf.writestr("autoself-agent/token", token + "\n")
        zf.writestr("autoself-agent/server", server + "\n")
        if os_name == "mac":
            # launcher .app (no Terminal); bake credentials into Resources
            app_root = static_desktop / "mac" / "AutoSelf.app"
            for path in app_root.rglob("*"):
                if path.is_file():
                    arc = "autoself-agent/AutoSelf助手.app/" + str(path.relative_to(app_root))
                    zf.write(path, arcname=arc)
            zf.writestr("autoself-agent/AutoSelf助手.app/Contents/Resources/token", token + "\n")
            zf.writestr("autoself-agent/AutoSelf助手.app/Contents/Resources/server", server + "\n")
            zf.writestr(
                "autoself-agent/AutoSelf助手.app/Contents/Resources/config.json",
                json.dumps({"server": server, "token": token}, ensure_ascii=False, indent=2),
            )
            install = """#!/bin/bash
cd "$(dirname "$0")"
mkdir -p "$HOME/.autoself"
cp -f token "$HOME/.autoself/token" 2>/dev/null || true
cp -f server "$HOME/.autoself/server" 2>/dev/null || true
cp -f config.json "$HOME/.autoself/config.json" 2>/dev/null || true
xattr -cr "AutoSelf助手.app" 2>/dev/null || true
open "AutoSelf助手.app"
/usr/bin/osascript >/dev/null 2>&1 <<'AS' &
tell application "Terminal"
  try
    close (every window whose name contains "双击启动" or name contains "autoself-agent") saving no
  end try
end tell
AS
exit 0
"""
            info = zipfile.ZipInfo("autoself-agent/双击启动本机助手.command")
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.create_system = 3  # Unix
            info.external_attr = 0o755 << 16
            zf.writestr(info, install)
            zf.writestr(
                "autoself-agent/说明.txt",
                "请只双击「AutoSelf助手」图标启动（不会弹出终端）。\n"
                "不要双击「双击启动本机助手.command」（那会打开终端窗口）。\n"
                "若系统拦截：右键图标 → 打开。\n"
                "看到右上角通知后，回网页点「我已启动，重新检测」。\n",
            )
        else:
            vbs_src = static_desktop / "win" / "启动AutoSelf.vbs"
            if vbs_src.exists():
                zf.write(vbs_src, arcname="autoself-agent/启动本机助手.vbs")
            install_bat = (
                "@echo off\r\n"
                "cd /d \"%~dp0\"\r\n"
                "start \"\" wscript \"%~dp0启动本机助手.vbs\"\r\n"
            )
            zf.writestr("autoself-agent/双击启动本机助手.bat", install_bat)
            zf.writestr(
                "autoself-agent/说明.txt",
                "推荐：双击「启动本机助手.vbs」（无黑窗口，后台运行）。\n"
                "也可双击「双击启动本机助手.bat」。\n"
                "弹出提示后回网页点「我已启动，重新检测」。\n",
            )

    data = buf.getvalue()
    filename = f"AutoSelf-agent-{os_name}.zip"
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/agent/heartbeat")
def api_agent_heartbeat():
    user_id = _try_current_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "未登录"}), 401
    data = request.get_json(force=True, silent=True) or {}
    agent_id = str(data.get("agent_id") or "").strip()
    if not agent_id:
        return jsonify({"ok": False, "error": "agent_id 必填"}), 400
    label = str(data.get("label") or "本机助手").strip() or "本机助手"
    entry = agent_store.heartbeat(user_id=user_id, agent_id=agent_id, label=label)
    return jsonify({"ok": True, "agent": entry})


@app.get("/api/agent/jobs/next")
@app.post("/api/agent/jobs/next")
def api_agent_next_job():
    user_id = _try_current_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "未登录"}), 401
    data = request.get_json(force=True, silent=True) or {}
    agent_id = (request.args.get("agent_id") or data.get("agent_id") or "").strip()
    if not agent_id:
        return jsonify({"ok": False, "error": "agent_id 必填"}), 400
    # refresh presence on poll
    agent_store.heartbeat(user_id=user_id, agent_id=agent_id)
    job = agent_store.claim_next_job(user_id=user_id, agent_id=agent_id)
    return jsonify({"ok": True, "job": job})


def _require_owned_job(job_id: str) -> tuple[dict | None, tuple | None]:
    user_id = _try_current_user_id()
    if not user_id:
        return None, (jsonify({"ok": False, "error": "未登录"}), 401)
    job = agent_store.get_job(job_id)
    if not job or int(job.get("user_id") or 0) != int(user_id):
        return None, (jsonify({"ok": False, "error": "任务不存在"}), 404)
    return job, None


@app.get("/api/agent/jobs/<job_id>")
@app.post("/api/agent/jobs/<job_id>")
def api_agent_get_job(job_id: str):
    job, err = _require_owned_job(job_id)
    if err:
        return err
    return jsonify({"ok": True, "job": job})


@app.post("/api/agent/jobs/<job_id>/qrcode")
def api_agent_job_qrcode(job_id: str):
    job, err = _require_owned_job(job_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    qrcode = data.get("qrcode")
    if not isinstance(qrcode, dict):
        return jsonify({"ok": False, "error": "qrcode 必填"}), 400
    updated = agent_store.set_job_qrcode(job_id, qrcode)
    return jsonify({"ok": True, "job": updated})


@app.post("/api/agent/jobs/<job_id>/event")
def api_agent_job_event(job_id: str):
    job, err = _require_owned_job(job_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    updated = agent_store.append_job_event(job_id, data)
    return jsonify({"ok": True, "job": updated})


@app.post("/api/agent/jobs/<job_id>/complete")
def api_agent_job_complete(job_id: str):
    job, err = _require_owned_job(job_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    cookie = result.get("cookie")
    if job.get("type") == "login" and isinstance(cookie, dict):
        _save_cookie_blob(job["platform"], job["account"], cookie)
        # avoid persisting full cookie blob in job history
        result = {k: v for k, v in result.items() if k != "cookie"}
        result.setdefault("success", True)
        result.setdefault("message", "登录成功")
    updated = agent_store.complete_job(job_id, result)
    return jsonify({"ok": True, "job": updated})


@app.post("/api/agent/jobs/<job_id>/fail")
def api_agent_job_fail(job_id: str):
    job, err = _require_owned_job(job_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    error = str(data.get("error") or "本机助手任务失败")
    updated = agent_store.fail_job(job_id, error)
    return jsonify({"ok": True, "job": updated})


@app.get("/api/agent/jobs/<job_id>/media")
@app.post("/api/agent/jobs/<job_id>/media")
def api_agent_job_media(job_id: str):
    # POST 优先：部分网络会劫持 HTTP GET 到备案拦截页 HTML。
    job, err = _require_owned_job(job_id)
    if err:
        return err
    media_path = Path(str((job.get("payload") or {}).get("media_path") or ""))
    if not media_path.is_file():
        return jsonify({"ok": False, "error": "媒体文件不存在"}), 404
    return send_file(media_path, as_attachment=True, download_name=media_path.name)


@app.post("/api/media/upload")
def media_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "缺少 file"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "文件名为空"}), 400
    safe = secure_filename(file.filename) or "video.mp4"
    file_id = uuid.uuid4().hex
    dest = MEDIA_DIR / f"{file_id}_{safe}"
    file.save(dest)
    return jsonify({"ok": True, "file_id": file_id, "filename": safe, "path": str(dest)})


@app.get("/api/tasks/<task_id>")
def get_task_progress(task_id: str):
    data = task_store.get_task(task_id)
    if not data:
        return jsonify({"ok": False, "error": "任务不存在或已过期"}), 404
    return jsonify({"ok": True, **data})


@app.post("/api/media-and-publish")
def media_and_publish():
    """Upload a local video (multipart), then publish in background with progress."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "请选择本地视频文件"}), 400
    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "文件名为空"}), 400

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    raw_tags = request.form.get("tags") or ""
    tags = parse_tags(raw_tags)
    default_tid = int(request.form.get("tid") or DEFAULT_BILIBILI_TID)

    raw_targets = request.form.get("targets") or "[]"
    try:
        targets_data = json.loads(raw_targets)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "targets 格式错误"}), 400
    if not isinstance(targets_data, dict):
        targets_data = {"targets": targets_data, "tid": default_tid}

    from web_mvp.targets import parse_targets_payload

    try:
        targets = parse_targets_payload(targets_data, default_tid=default_tid)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not targets:
        return jsonify({"ok": False, "error": "请至少选择一个上传目标账号"}), 400
    if not title:
        return jsonify({"ok": False, "error": "请填写标题"}), 400
    if not description:
        return jsonify({"ok": False, "error": "请填写文案"}), 400

    safe = secure_filename(file.filename) or "video.mp4"
    file_id = uuid.uuid4().hex
    dest = MEDIA_DIR / f"{file_id}_{safe}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    file.save(dest)

    publish_description = description
    schedule = parse_schedule(request.form.get("schedule"))
    task_id = task_store.create_task("文件已接收，准备发布…")
    uid = _try_current_user_id()

    def _work():
        try:
            results: list[dict] = []
            n = max(1, len(targets))
            for i, target in enumerate(targets):
                platform = target["platform"]
                account = target["account"]
                label = PLATFORM_LABELS.get(platform, platform)
                pct = 10 + int(80 * i / n)
                task_store.update_task(task_id, pct, f"正在上传到 {label}/{account}…")
                try:
                    _publish_to_platform(
                        platform=platform,
                        account=account,
                        video_path=dest,
                        title=title,
                        description=publish_description,
                        tags=tags,
                        tid=int(target.get("tid") or default_tid),
                        schedule=schedule,
                    )
                    results.append({"ok": True, "platform": platform, "account": account})
                except Exception as exc:
                    results.append(
                        {
                            "ok": False,
                            "platform": platform,
                            "account": account,
                            "error": _friendly_publish_error(exc),
                        }
                    )
            any_ok = any(item.get("ok") for item in results)
            history_store.append_event(
                source="local",
                title=title,
                results=results,
                detail="" if any_ok else "全部目标上传失败",
                source_ref=safe,
                user_id=uid,
            )
            payload = {
                "ok": any_ok,
                "file_id": file_id,
                "filename": safe,
                "path": str(dest),
                "publish_title": title,
                "publish_description": publish_description,
                "tags": tags,
                "results": results,
                "error": None if any_ok else "全部目标上传失败",
            }
            task_store.update_task(task_id, 100, "发布完成" if any_ok else "发布失败")
            task_store.complete_task(task_id, payload)
        except Exception as exc:
            history_store.append_event(
                source="local",
                title=title,
                status="failed",
                detail=str(exc)[-500:],
                source_ref=safe,
                user_id=uid,
            )
            task_store.fail_task(task_id, str(exc))

    threading.Thread(target=_work, name=f"publish-{task_id[:8]}", daemon=True).start()
    return jsonify(
        {
            "ok": True,
            "job_id": task_id,
            "file_id": file_id,
            "filename": safe,
            "path": str(dest),
            "publish_title": title,
            "publish_description": publish_description,
            "tags": tags,
        }
    )


def _resolve_media_path(file_id: str | None, path: str | None) -> Path:
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = Path(BASE_DIR) / p
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {p}")
        return p
    if not file_id:
        raise ValueError("需要 file_id 或 path")
    matches = list(MEDIA_DIR.glob(f"{file_id}_*"))
    if not matches:
        raise FileNotFoundError(f"找不到 file_id={file_id}")
    return matches[0]


async def _publish_douyin(account: str, video_path: Path, title: str, description: str, tags: list[str], schedule):
    account_file = resolve_account_file("douyin", account)
    ready = await douyin_setup(str(account_file), handle=False)
    if not ready:
        raise RuntimeError(f"抖音 Cookie 无效，请先登录账号 {account}")
    app_upload = DouYinVideo(
        title,
        str(video_path),
        tags,
        schedule,
        str(account_file),
        desc=description,
        headless=True,
    )
    await app_upload.douyin_upload_video()


async def _publish_kuaishou(account: str, video_path: Path, title: str, description: str, tags: list[str], schedule):
    account_file = resolve_account_file("kuaishou", account)
    ready = await ks_setup(str(account_file), handle=False)
    if not ready:
        raise RuntimeError(f"快手 Cookie 无效，请先登录账号 {account}")
    app_upload = KSVideo(
        title=title,
        file_path=str(video_path),
        desc=description,
        tags=tags,
        publish_date=schedule,
        account_file=str(account_file),
        headless=True,
    )
    await app_upload.main()


def _friendly_publish_error(exc: BaseException | str) -> str:
    """Strip biliup ANSI noise; surface the useful Chinese/API message."""
    raw = str(exc or "").strip()
    if not raw:
        return "上传失败"
    text = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    for pat in (
        r'message:\s*"([^"]+)"',
        r"message:\s*'([^']+)'",
        r"投稿过于频繁[^\"'\n]*",
    ):
        m = re.search(pat, text)
        if m:
            return (m.group(1) if m.lastindex else m.group(0)).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    useful = [
        ln
        for ln in lines
        if "crates/" not in ln
        and not ln.startswith(("├", "╰", "│"))
        and "Unknown Error" not in ln
    ]
    if useful:
        msg = useful[-1]
        return (msg[:200] + "…") if len(msg) > 200 else msg
    return text[:200]


def _publish_bilibili(account: str, video_path: Path, title: str, description: str, tags: list[str], schedule, tid: int):
    account_file = resolve_account_file("bilibili", account)
    if not account_file.exists():
        raise RuntimeError(f"B站账号文件不存在，请先在网页扫码登录账号 {account}")
    from uploader.bilibili_uploader.qr_login import ensure_biliup_login_info

    ensure_biliup_login_info(account_file)
    arguments = [
        "-u",
        str(account_file),
        "upload",
        str(video_path),
        "--title",
        title,
        "--desc",
        description or title,
        "--tid",
        str(tid),
        "--line",
        DEFAULT_BILIBILI_UPLOAD_LINE,
    ]
    if tags:
        arguments.extend(["--tag", ",".join(tags)])
    if isinstance(schedule, datetime):
        arguments.extend(["--dtime", str(int(schedule.timestamp()))])
    result = run_biliup_command(arguments)
    if result.returncode != 0:
        raise RuntimeError(
            _friendly_publish_error((result.stderr or result.stdout or "").strip() or "B站上传失败")
        )


async def _publish_tencent(account: str, video_path: Path, title: str, description: str, tags: list[str], schedule):
    account_file = resolve_account_file("tencent", account)
    ready = await tencent_setup(str(account_file), handle=False)
    if not ready:
        raise RuntimeError(f"微信视频号 Cookie 无效，请先登录账号 {account}")
    if isinstance(schedule, datetime):
        publish_date = schedule
        strategy = TENCENT_PUBLISH_STRATEGY_SCHEDULED
    else:
        publish_date = 0
        strategy = TENCENT_PUBLISH_STRATEGY_IMMEDIATE
    app_upload = TencentVideo(
        title=title,
        file_path=str(video_path),
        tags=tags,
        publish_date=publish_date,
        account_file=str(account_file),
        desc=description,
        publish_strategy=strategy,
        headless=True,
    )
    await app_upload.main()


def _publish_to_platform(
    *,
    platform: str,
    account: str,
    video_path: Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    tid: int = DEFAULT_BILIBILI_TID,
    schedule: datetime | int = 0,
) -> None:
    tags = tags or []
    user_id = _try_current_user_id()
    if (
        user_id
        and platform in AGENT_PLATFORMS
        and agent_store.is_agent_online(user_id)
    ):
        _publish_via_agent(
            user_id=user_id,
            platform=platform,
            account=account,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            schedule=schedule,
        )
        return
    if platform == "douyin":
        _run_async(_publish_douyin(account, video_path, title, description, tags, schedule))
    elif platform == "kuaishou":
        _run_async(_publish_kuaishou(account, video_path, title, description, tags, schedule))
    elif platform == "bilibili":
        _publish_bilibili(account, video_path, title, description, tags, schedule, tid)
    elif platform == "tencent":
        _run_async(_publish_tencent(account, video_path, title, description, tags, schedule))
    else:
        raise ValueError(f"不支持的平台: {platform}")


sub_store.set_publish_fn(_publish_to_platform)
folder_store.set_publish_fn(_publish_to_platform)
# Start daily schedulers in the worker process (has publish_fn). Only instance
# autoself@5410 sets AUTOSELF_ENABLE_SCHEDULERS=1 so we don't double-run.
if os.environ.get("AUTOSELF_ENABLE_SCHEDULERS", "0") == "1":
    sub_store.start_scheduler()
    folder_store.start_scheduler()


@app.post("/api/publish")
def publish():
    data = request.get_json(force=True, silent=True) or {}
    platform = (data.get("platform") or "").strip()
    account = (data.get("account") or "").strip()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = parse_tags(tags)
    elif not isinstance(tags, list):
        tags = []
    tags = [str(t).strip().lstrip("#") for t in tags if str(t).strip()]

    if platform not in PLATFORMS:
        return jsonify({"ok": False, "error": "platform 仅支持 douyin/kuaishou/bilibili/tencent"}), 400
    if not account or not title:
        return jsonify({"ok": False, "error": "account / title 必填"}), 400

    try:
        video_path = _resolve_media_path(data.get("file_id"), data.get("path"))
        schedule = parse_schedule(data.get("schedule"))
        _publish_to_platform(
            platform=platform,
            account=account,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            tid=int(data.get("tid") or DEFAULT_BILIBILI_TID),
            schedule=schedule,
        )
        history_store.append_event(
            source="publish",
            title=title,
            results=[{"ok": True, "platform": platform, "account": account}],
            source_ref=str(video_path),
            user_id=_try_current_user_id(),
        )
        return jsonify({"ok": True, "platform": platform, "account": account, "file": str(video_path)})
    except Exception as exc:
        history_store.append_event(
            source="publish",
            title=title,
            results=[{"ok": False, "platform": platform, "account": account, "error": str(exc)}],
            detail=str(exc),
            source_ref=str(data.get("path") or data.get("file_id") or ""),
            user_id=_try_current_user_id(),
        )
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/publish-history")
def publish_history():
    limit = request.args.get("limit", 50)
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 50
    return jsonify({"ok": True, "events": history_store.list_events(limit_n)})


@app.get("/api/subscriptions")
def list_subscriptions():
    from web_mvp.targets import targets_from_record, targets_summary

    items = []
    for item in sub_store.load_subscriptions():
        row = dict(item)
        targets = targets_from_record(row)
        row["targets"] = targets
        row["targets_summary"] = targets_summary(targets)
        items.append(row)
    return jsonify({"ok": True, "subscriptions": items})


@app.post("/api/subscriptions")
def create_subscription():
    data = request.get_json(force=True, silent=True) or {}
    from web_mvp.targets import parse_targets_payload

    homepage = (data.get("homepage_url") or data.get("url") or "").strip()
    if not homepage:
        return jsonify({"ok": False, "error": "请填写博主主页链接"}), 400
    targets = parse_targets_payload(data)
    if not targets:
        return jsonify({"ok": False, "error": "请至少选择一个上传目标账号"}), 400
    try:
        item = sub_store.add_subscription(
            homepage_url=homepage,
            daily_time=data.get("daily_time") or data.get("time") or "",
            targets=targets,
            tid=int(data.get("tid") or DEFAULT_BILIBILI_TID),
        )
        return jsonify({"ok": True, "subscription": item})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.delete("/api/subscriptions/<sub_id>")
def remove_subscription(sub_id: str):
    ok = sub_store.delete_subscription(sub_id)
    if not ok:
        return jsonify({"ok": False, "error": "订阅不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/subscriptions/<sub_id>/toggle")
def toggle_subscription(sub_id: str):
    data = request.get_json(force=True, silent=True) or {}
    enabled = data.get("enabled")
    if enabled is None:
        return jsonify({"ok": False, "error": "需要 enabled"}), 400
    item = sub_store.set_enabled(sub_id, bool(enabled))
    if not item:
        return jsonify({"ok": False, "error": "订阅不存在"}), 404
    return jsonify({"ok": True, "subscription": item})


@app.post("/api/subscriptions/<sub_id>/run")
def run_subscription_now(sub_id: str):
    force = True
    data = request.get_json(force=True, silent=True) or {}
    if "force" in data:
        force = bool(data.get("force"))
    try:
        result = sub_store.run_subscription(sub_id, force=force)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/folder-queues")
def list_folder_queues():
    items = [folder_store.enrich_queue(x) for x in folder_store.load_queues()]
    return jsonify({"ok": True, "queues": items, "default_copy": folder_store.DEFAULT_COPY})


@app.post("/api/folder-queues")
def create_folder_queue():
    data = request.get_json(force=True, silent=True) or {}
    from web_mvp.targets import parse_targets_payload

    folder_path = (data.get("folder_path") or data.get("path") or "").strip()
    if not folder_path:
        return jsonify({"ok": False, "error": "请先选择要监控的文件夹"}), 400
    targets = parse_targets_payload(data)
    if not targets:
        return jsonify({"ok": False, "error": "请至少选择一个上传目标账号"}), 400
    try:
        item = folder_store.add_queue(
            folder_path=folder_path,
            daily_time=data.get("daily_time") or data.get("time") or "",
            targets=targets,
            tid=int(data.get("tid") or DEFAULT_BILIBILI_TID),
        )
        return jsonify({"ok": True, "queue": item})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.delete("/api/folder-queues/<queue_id>")
def remove_folder_queue(queue_id: str):
    ok = folder_store.delete_queue(queue_id)
    if not ok:
        return jsonify({"ok": False, "error": "队列不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/folder-queues/<queue_id>/toggle")
def toggle_folder_queue(queue_id: str):
    data = request.get_json(force=True, silent=True) or {}
    if "enabled" not in data:
        return jsonify({"ok": False, "error": "需要 enabled"}), 400
    item = folder_store.set_enabled(queue_id, bool(data.get("enabled")))
    if not item:
        return jsonify({"ok": False, "error": "队列不存在"}), 404
    return jsonify({"ok": True, "queue": item})


@app.put("/api/folder-queues/<queue_id>/drafts")
def save_folder_queue_drafts(queue_id: str):
    data = request.get_json(force=True, silent=True) or {}
    drafts = data.get("drafts") or {}
    if not isinstance(drafts, dict):
        return jsonify({"ok": False, "error": "drafts 应为对象"}), 400
    item = folder_store.update_drafts(queue_id, drafts)
    if not item:
        return jsonify({"ok": False, "error": "队列不存在"}), 404
    return jsonify({"ok": True, "queue": item})


@app.post("/api/folder-queues/<queue_id>/run")
def run_folder_queue_now(queue_id: str):
    try:
        result = folder_store.run_queue(queue_id, force=True)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/download")
def create_download():
    data = request.get_json(force=True, silent=True) or {}
    raw_url = data.get("url") or ""
    try:
        result = download_share_url(raw_url, output_dir=DOWNLOADS_DIR / uuid.uuid4().hex)
        meta = {
            "download_id": result.download_id,
            "platform": result.platform,
            "source_url": result.source_url,
            "title": result.title,
            "filename": result.filename,
            "path": str(result.file_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        _DOWNLOAD_INDEX[result.download_id] = meta
        return jsonify({"ok": True, **meta, "download_url": f"/api/download/{result.download_id}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/download-and-publish")
def download_and_publish():
    """Parse share URL → download once → upload; returns job_id for progress polling."""
    data = request.get_json(force=True, silent=True) or {}
    raw_url = data.get("url") or ""
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = parse_tags(tags)
    elif not isinstance(tags, list):
        tags = []
    tags = [str(t).strip().lstrip("#") for t in tags if str(t).strip()]
    default_tid = int(data.get("tid") or DEFAULT_BILIBILI_TID)

    from web_mvp.targets import parse_targets_payload

    try:
        targets = parse_targets_payload(data, default_tid=default_tid)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not targets:
        return jsonify({"ok": False, "error": "请至少选择一个上传目标账号"}), 400

    schedule = parse_schedule(data.get("schedule"))
    task_id = task_store.create_task("解析链接中…")
    uid = _try_current_user_id()

    def _work():
        stop_hb = threading.Event()

        def _heartbeat(lo: int, hi: int, message: str):
            pct = lo
            while not stop_hb.wait(1.0):
                if pct < hi:
                    pct += 1
                    task_store.update_task(task_id, pct, message)

        try:
            task_store.update_task(task_id, 5, "解析并下载视频中…")
            hb = threading.Thread(
                target=_heartbeat,
                args=(5, 32, "解析并下载视频中…"),
                daemon=True,
            )
            hb.start()
            result = download_share_url(raw_url, output_dir=DOWNLOADS_DIR / uuid.uuid4().hex)
            stop_hb.set()
            meta = {
                "download_id": result.download_id,
                "source_platform": result.platform,
                "source_url": result.source_url,
                "title": result.title,
                "filename": result.filename,
                "path": str(result.file_path),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            _DOWNLOAD_INDEX[result.download_id] = meta

            publish_title = title or result.title or Path(result.file_path).stem
            publish_description = description or result.title or publish_title
            task_store.update_task(task_id, 35, f"已下载：{result.filename or '视频'}，准备上传…")

            results: list[dict] = []
            n = max(1, len(targets))
            for i, target in enumerate(targets):
                platform = target["platform"]
                account = target["account"]
                label = PLATFORM_LABELS.get(platform, platform)
                base = 35 + int(55 * i / n)
                top = 35 + int(55 * (i + 1) / n) - 1
                stop_hb.clear()
                hb = threading.Thread(
                    target=_heartbeat,
                    args=(base, max(base + 1, top), f"正在上传到 {label}/{account}…"),
                    daemon=True,
                )
                task_store.update_task(task_id, base, f"正在上传到 {label}/{account}…")
                hb.start()
                try:
                    _publish_to_platform(
                        platform=platform,
                        account=account,
                        video_path=result.file_path,
                        title=publish_title,
                        description=publish_description,
                        tags=tags,
                        tid=int(target.get("tid") or default_tid),
                        schedule=schedule,
                    )
                    results.append({"ok": True, "platform": platform, "account": account})
                except Exception as exc:
                    results.append(
                        {
                            "ok": False,
                            "platform": platform,
                            "account": account,
                            "error": _friendly_publish_error(exc),
                        }
                    )
                finally:
                    stop_hb.set()

            any_ok = any(item.get("ok") for item in results)
            history_store.append_event(
                source="link",
                title=publish_title,
                results=results,
                detail="" if any_ok else "全部目标上传失败",
                source_ref=result.source_url or raw_url,
                user_id=uid,
            )
            payload = {
                "ok": any_ok,
                **meta,
                "publish_title": publish_title,
                "publish_description": publish_description,
                "download_url": f"/api/download/{result.download_id}",
                "results": results,
                "upload_platform": results[0]["platform"] if len(results) == 1 else "",
                "upload_account": results[0]["account"] if len(results) == 1 else "",
                "error": None if any_ok else "全部目标上传失败",
            }
            task_store.update_task(
                task_id, 100, "上传完成" if any_ok else "上传失败"
            )
            task_store.complete_task(task_id, payload)
        except Exception as exc:
            stop_hb.set()
            history_store.append_event(
                source="link",
                title=(title or "").strip(),
                status="failed",
                detail=str(exc)[-500:],
                source_ref=raw_url,
                user_id=uid,
            )
            task_store.fail_task(task_id, str(exc))

    threading.Thread(target=_work, name=f"dlpub-{task_id[:8]}", daemon=True).start()
    return jsonify({"ok": True, "job_id": task_id})


@app.get("/api/download/<download_id>")
def get_download(download_id: str):
    meta = _DOWNLOAD_INDEX.get(download_id)
    path: Path | None = None
    filename = "video.mp4"
    if meta:
        path = Path(meta["path"])
        filename = meta.get("filename") or path.name
    else:
        folder = DOWNLOADS_DIR / download_id
        if folder.exists():
            candidates = sorted(
                [p for p in folder.iterdir() if p.is_file()],
                key=lambda p: p.stat().st_size,
                reverse=True,
            )
            if candidates:
                path = candidates[0]
                filename = path.name
    if path is None or not path.exists():
        return jsonify({"ok": False, "error": "文件不存在或已过期"}), 404
    return send_file(path, as_attachment=True, download_name=filename)


@app.post("/api/cleanup")
def cleanup_tmp():
    removed = 0
    for root in (MEDIA_DIR, DOWNLOADS_DIR):
        if not root.exists():
            continue
        for child in root.iterdir():
            try:
                if child.is_file():
                    child.unlink()
                    removed += 1
                elif child.is_dir():
                    shutil.rmtree(child)
                    removed += 1
            except OSError:
                continue
    _DOWNLOAD_INDEX.clear()
    return jsonify({"ok": True, "removed": removed})


def create_app() -> Flask:
    return app


def run_server(host: str = "0.0.0.0", port: int = 5410, *, debug: bool = True) -> None:
    sub_store.start_scheduler()
    folder_store.start_scheduler()
    # Disable reloader so the daily scheduler is not started twice.
    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)


def main():
    run_server()


if __name__ == "__main__":
    main()
