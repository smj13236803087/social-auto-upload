"""CN-only Web MVP: bind accounts + upload, paste share URL + download."""

from __future__ import annotations

import asyncio
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
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
from web_mvp import folder_queue as folder_store
from web_mvp import publish_history as history_store
from web_mvp import subscriptions as sub_store
from web_mvp.account_profile import resolve_account_profile
from web_mvp.auth import AuthError, extract_bearer, login as auth_login
from web_mvp.auth import admin_login as auth_admin_login
from web_mvp.auth import ensure_admin_user, logout as auth_logout
from web_mvp.auth import resolve_token
from web_mvp import admin_users as admin_user_store
from web_mvp.bili_partitions import DEFAULT_BILIBILI_TID, list_bilibili_partitions

PLATFORMS = ("douyin", "kuaishou", "bilibili", "tencent")
PLATFORM_LABELS = {"douyin": "抖音", "kuaishou": "快手", "bilibili": "B站", "tencent": "微信视频号"}
SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"
DEFAULT_BILIBILI_UPLOAD_LINE = "bda2"
AUTH_PUBLIC_PREFIXES = (
    "/static/",
    "/api/health",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
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


def _run_async(coro):
    return asyncio.run(coro)


def resolve_account_file(platform: str, account_name: str) -> Path:
    account_file = Path(BASE_DIR) / "cookies" / f"{platform}_{account_name}.json"
    account_file.parent.mkdir(exist_ok=True)
    return account_file


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


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/admin")
@app.get("/admin/")
def admin_index():
    return send_from_directory(STATIC_DIR, "admin.html")


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
    return jsonify({"ok": True, "platforms": list(PLATFORMS)})


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
    return jsonify({"accounts": accounts})


@app.get("/api/accounts/check")
def check_account():
    platform = (request.args.get("platform") or "").strip()
    account = (request.args.get("account") or "").strip()
    if platform not in PLATFORMS or not account:
        return jsonify({"ok": False, "error": "platform / account 必填"}), 400
    try:
        valid = _check_account(platform, account)
        return jsonify({"ok": True, "platform": platform, "account": account, "valid": valid})
    except Exception as exc:
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


@app.get("/api/accounts/login")
def login_account_sse():
    """SSE login for douyin/kuaishou/bilibili/tencent (QR in browser)."""
    platform = (request.args.get("platform") or "").strip()
    account = (request.args.get("account") or "").strip()
    headed = (request.args.get("headed") or "0") in ("1", "true", "yes")
    if platform not in PLATFORMS or not account:
        return jsonify({"ok": False, "error": "platform / account 必填"}), 400

    events: queue.Queue = queue.Queue()

    def qrcode_callback(payload: dict):
        events.put({"event": "qrcode", "qrcode": payload})

    def worker():
        try:
            account_file = str(resolve_account_file(platform, account))
            headless = not headed
            if platform == "douyin":
                result = _run_async(
                    douyin_setup(
                        account_file,
                        handle=True,
                        return_detail=True,
                        qrcode_callback=qrcode_callback,
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
                )
            elif platform == "tencent":
                # Web UI shows the QR; keep browser headless (no popup Chrome window).
                result = _run_async(
                    tencent_setup(
                        account_file,
                        handle=True,
                        return_detail=True,
                        qrcode_callback=qrcode_callback,
                        headless=True,
                    )
                )
            else:
                raise ValueError(f"不支持的平台: {platform}")
            events.put({"event": "done", "result": result})
        except Exception as exc:
            events.put({"event": "error", "message": str(exc)})

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            try:
                item = events.get(timeout=300)
            except queue.Empty:
                yield f"data: {json.dumps({'event': 'error', 'message': '登录超时'}, ensure_ascii=False)}\n\n"
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if item.get("event") in ("done", "error"):
                break

    return Response(stream(), mimetype="text/event-stream")


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


@app.post("/api/media-and-publish")
def media_and_publish():
    """Upload a local video (multipart) then publish to one or more bound accounts."""
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

    safe = secure_filename(file.filename) or "video.mp4"
    file_id = uuid.uuid4().hex
    dest = MEDIA_DIR / f"{file_id}_{safe}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    file.save(dest)

    publish_description = description or title
    schedule = parse_schedule(request.form.get("schedule"))
    results: list[dict] = []
    for target in targets:
        platform = target["platform"]
        account = target["account"]
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
                    "error": str(exc),
                }
            )

    any_ok = any(item.get("ok") for item in results)
    history_store.append_event(
        source="local",
        title=title,
        results=results,
        detail="" if any_ok else "全部目标上传失败",
        source_ref=safe,
    )
    return jsonify(
        {
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


def _publish_bilibili(account: str, video_path: Path, title: str, description: str, tags: list[str], schedule, tid: int):
    account_file = resolve_account_file("bilibili", account)
    if not account_file.exists():
        raise RuntimeError(f"B站账号文件不存在，请先在网页扫码登录账号 {account}")
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
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "B站上传失败")


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
        )
        return jsonify({"ok": True, "platform": platform, "account": account, "file": str(video_path)})
    except Exception as exc:
        history_store.append_event(
            source="publish",
            title=title,
            results=[{"ok": False, "platform": platform, "account": account, "error": str(exc)}],
            detail=str(exc),
            source_ref=str(data.get("path") or data.get("file_id") or ""),
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
    """Parse share URL → download once → upload to one or more platform accounts."""
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

    try:
        result = download_share_url(raw_url, output_dir=DOWNLOADS_DIR / uuid.uuid4().hex)
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
        schedule = parse_schedule(data.get("schedule"))

        results: list[dict] = []
        for target in targets:
            platform = target["platform"]
            account = target["account"]
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
                        "error": str(exc),
                    }
                )

        any_ok = any(item.get("ok") for item in results)
        history_store.append_event(
            source="link",
            title=publish_title,
            results=results,
            detail="" if any_ok else "全部目标上传失败",
            source_ref=result.source_url or raw_url,
        )
        return jsonify(
            {
                "ok": any_ok,
                **meta,
                "publish_title": publish_title,
                "publish_description": publish_description,
                "download_url": f"/api/download/{result.download_id}",
                "results": results,
                # Keep old fields for single-target callers
                "upload_platform": results[0]["platform"] if len(results) == 1 else "",
                "upload_account": results[0]["account"] if len(results) == 1 else "",
                "error": None if any_ok else "全部目标上传失败",
            }
        ), (200 if any_ok else 500)
    except Exception as exc:
        history_store.append_event(
            source="link",
            title=(title or "").strip(),
            status="failed",
            detail=str(exc)[-500:],
            source_ref=raw_url,
        )
        return jsonify({"ok": False, "error": str(exc)}), 500


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
