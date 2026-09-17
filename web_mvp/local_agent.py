"""Local AutoSelf agent: pull cloud jobs and run Douyin/Kuaishou on this machine.

Usage:
  export AUTOSELF_SERVER=https://autopost.com.cn
  export AUTOSELF_TOKEN='your-bearer-token'
  python -m web_mvp.local_agent

Or:
  python -m web_mvp.local_agent --server https://autopost.com.cn --token ...
"""

from __future__ import annotations

import argparse
import threading
from datetime import datetime
import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import requests

from conf import BASE_DIR


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _normalize_server(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    return raw or "http://autopost.com.cn"


def _http_fallback(url: str) -> str | None:
    if url.startswith("https://"):
        return "http://" + url[len("https://") :]
    return None


class AgentClient:
    def __init__(self, server: str, token: str, agent_id: str, label: str):
        self.server = _normalize_server(server)
        self.token = token
        self.agent_id = agent_id
        self.label = label
        self.session = requests.Session()
        # Avoid broken local https_proxy breaking cloud calls.
        self.session.trust_env = False

    def _url(self, path: str) -> str:
        return f"{self.server}{path}"

    def _request(self, method: str, path: str, **kwargs):
        timeout = kwargs.pop("timeout", 20)
        try:
            r = self.session.request(method, self._url(path), timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception as first_exc:
            alt = _http_fallback(self.server)
            if not alt or alt == self.server:
                raise first_exc
            print(f"[agent] {self.server} 不通，改用 {alt}", flush=True)
            self.server = alt
            r = self.session.request(method, self._url(path), timeout=timeout, **kwargs)
            r.raise_for_status()
            return r

    def _json(self, r):
        ctype = (r.headers.get("content-type") or "").lower()
        text = r.text or ""
        if "application/json" not in ctype and not text.lstrip().startswith("{"):
            raise RuntimeError(f"云端返回非 JSON（可能被运营商劫持）: {text[:120]}")
        return r.json()

    def heartbeat(self) -> dict:
        r = self._request(
            "POST",
            "/api/agent/heartbeat",
            headers=_headers(self.token),
            json={"agent_id": self.agent_id, "label": self.label},
            timeout=20,
        )
        return self._json(r)

    def next_job(self) -> dict | None:
        # Use POST: some networks hijack plain HTTP GET to ICP interstitial HTML.
        r = self._request(
            "POST",
            "/api/agent/jobs/next",
            headers=_headers(self.token),
            json={"agent_id": self.agent_id},
            timeout=20,
        )
        return (self._json(r) or {}).get("job")

    def get_job(self, job_id: str) -> dict | None:
        r = self._request(
            "POST",
            f"/api/agent/jobs/{job_id}",
            headers=_headers(self.token),
            json={},
            timeout=20,
        )
        return (self._json(r) or {}).get("job")

    def post_qrcode(self, job_id: str, qrcode: dict) -> None:
        self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/qrcode",
            headers=_headers(self.token),
            json={"qrcode": qrcode},
            timeout=20,
        )

    def post_event(self, job_id: str, event: dict) -> None:
        self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/event",
            headers=_headers(self.token),
            json=event,
            timeout=20,
        )

    def complete(self, job_id: str, result: dict) -> None:
        self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/complete",
            headers=_headers(self.token),
            json={"result": result},
            timeout=60,
        )

    def fail(self, job_id: str, error: str) -> None:
        self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/fail",
            headers=_headers(self.token),
            json={"error": error},
            timeout=20,
        )

    def download_media(self, job_id: str, dest: Path) -> Path:
        # Must use POST: HTTP GET to this domain is often hijacked to ICP HTML.
        r = self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/media",
            headers=_headers(self.token),
            json={},
            timeout=600,
            stream=True,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        head = dest.read_bytes()[:64]
        lowered = head.lstrip().lower()
        if lowered.startswith((b"<!doctype", b"<html", b"{")) or b"ftyp" not in head:
            size = dest.stat().st_size
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"下载到的不是有效视频（{size} 字节，可能被网络劫持），请重试或换网络"
            )
        return dest


async def _run_login(client: AgentClient, job: dict) -> None:
    platform = job["platform"]
    account = job["account"]
    job_id = job["id"]
    account_file = Path(BASE_DIR) / "cookies" / f"{platform}_{account}.json"
    account_file.parent.mkdir(parents=True, exist_ok=True)
    code_file = account_file.with_name(f"{account_file.stem}_verify_code.txt")
    stop_poll = threading.Event()

    def cancel_check() -> bool:
        try:
            cur = client.get_job(job_id) or {}
            return cur.get("status") in {"cancelled", "error", "done"}
        except Exception:
            return False

    def _poll_verify_code():
        last = ""
        while not stop_poll.wait(1.5):
            try:
                cur = client.get_job(job_id) or {}
                code = str(cur.get("verify_code") or "").strip()
                if code and code != last:
                    code_file.write_text(code, encoding="utf-8")
                    last = code
                    print(f"[agent] got verify code for {platform}/{account}", flush=True)
                if cur.get("status") in {"done", "error", "cancelled"}:
                    break
            except Exception as exc:
                print(f"[agent] poll verify failed: {exc}", flush=True)

    def qrcode_callback(payload: dict):
        try:
            client.post_qrcode(job_id, payload)
        except Exception as exc:
            print(f"[agent] push qrcode failed: {exc}", flush=True)

    def event_callback(payload: dict):
        try:
            client.post_event(job_id, payload)
        except Exception as exc:
            print(f"[agent] push event failed: {exc}", flush=True)

    poller = threading.Thread(target=_poll_verify_code, daemon=True)
    poller.start()
    try:
        if platform == "douyin":
            from uploader.douyin_uploader.main import douyin_setup

            try:
                event_callback({"event": "status", "message": "本机助手正在打开抖音登录页…"})
            except Exception:
                pass
            result = await douyin_setup(
                str(account_file),
                handle=True,
                return_detail=True,
                qrcode_callback=qrcode_callback,
                event_callback=event_callback,
                cancel_check=cancel_check,
                headless=True,
                risk_fail_fast=False,
            )
        elif platform == "kuaishou":
            from uploader.ks_uploader.main import ks_setup

            result = await ks_setup(
                str(account_file),
                handle=True,
                return_detail=True,
                qrcode_callback=qrcode_callback,
                headless=True,
            )
        else:
            raise RuntimeError(f"本机助手暂不支持平台: {platform}")
    finally:
        stop_poll.set()

    if cancel_check():
        raise RuntimeError("登录已取消（已有新的扫码任务）")
    if not result.get("success"):
        raise RuntimeError(result.get("message") or "登录失败")

    cookie_blob = json.loads(account_file.read_text(encoding="utf-8"))
    client.complete(
        job_id,
        {
            "success": True,
            "message": result.get("message") or "登录成功",
            "account": account,
            "platform": platform,
            "cookie": cookie_blob,
        },
    )




def _parse_schedule(raw) -> datetime | int:
    if raw in (None, "", 0, "0"):
        return 0
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) if raw else 0
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return 0


async def _run_publish(client: AgentClient, job: dict) -> None:
    platform = job["platform"]
    account = job["account"]
    job_id = job["id"]
    payload = job.get("payload") or {}
    title = payload.get("title") or ""
    description = payload.get("description") or title
    tags = payload.get("tags") or []
    schedule_raw = _parse_schedule(payload.get("schedule") or 0)

    cookie = payload.get("cookie")
    if not isinstance(cookie, dict):
        raise RuntimeError("任务缺少 Cookie，请先完成本机扫码登录")

    with tempfile.TemporaryDirectory(prefix="autoself_agent_") as tmp:
        tmp_path = Path(tmp)
        account_file = tmp_path / f"{platform}_{account}.json"
        account_file.write_text(json.dumps(cookie, ensure_ascii=False), encoding="utf-8")
        media_name = payload.get("filename") or "video.mp4"
        video_path = tmp_path / media_name
        client.download_media(job_id, video_path)

        if platform == "douyin":
            from uploader.douyin_uploader.main import DouYinVideo, douyin_setup

            ready = await douyin_setup(str(account_file), handle=False)
            if not ready:
                raise RuntimeError("抖音 Cookie 无效，请重新扫码登录")
            uploader = DouYinVideo(
                title,
                str(video_path),
                tags,
                schedule_raw or 0,
                str(account_file),
                desc=description,
                headless=True,
            )
            await uploader.douyin_upload_video()
        elif platform == "kuaishou":
            from uploader.ks_uploader.main import KSVideo, ks_setup

            ready = await ks_setup(str(account_file), handle=False)
            if not ready:
                raise RuntimeError("快手 Cookie 无效，请重新扫码登录")
            uploader = KSVideo(
                title,
                str(video_path),
                tags,
                schedule_raw or 0,
                str(account_file),
                desc=description,
                headless=True,
            )
            await uploader.main()
        else:
            raise RuntimeError(f"本机助手暂不支持发布平台: {platform}")

    client.complete(
        job_id,
        {
            "success": True,
            "message": "发布成功",
            "account": account,
            "platform": platform,
        },
    )


def _pid_lock_path() -> Path:
    return Path.home() / ".autoself" / "agent.pid"


def _acquire_singleton() -> bool:
    path = _pid_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    if path.exists():
        try:
            old = int(path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            old = 0
        if old and old != my_pid:
            try:
                os.kill(old, 0)
                print(f"[agent] 已有助手在运行 (pid={old})，本次退出", flush=True)
                return False
            except OSError:
                pass
    path.write_text(str(my_pid), encoding="utf-8")
    return True


def run_forever(server: str, token: str, label: str) -> None:
    if not _acquire_singleton():
        return
    agent_id = os.environ.get("AUTOSELF_AGENT_ID") or f"agent_{uuid.uuid4().hex[:10]}"
    client = AgentClient(server, token, agent_id, label)
    print(f"[agent] server={server} id={agent_id}", flush=True)
    print("[agent] 保持运行；云端抖音登录与发布会自动派到本机执行", flush=True)

    stop_hb = threading.Event()

    def _hb_loop():
        while not stop_hb.wait(10):
            try:
                client.heartbeat()
            except Exception as exc:
                print(f"[agent] heartbeat error: {exc}", flush=True)

    hb_thread = threading.Thread(target=_hb_loop, daemon=True)
    hb_thread.start()

    try:
        while True:
            try:
                client.heartbeat()
                job = client.next_job()
                if not job:
                    time.sleep(2)
                    continue
                job_id = job["id"]
                print(f"[agent] got job {job_id} type={job.get('type')} platform={job.get('platform')}", flush=True)
                try:
                    if job.get("type") == "login":
                        asyncio.run(_run_login(client, job))
                    elif job.get("type") == "publish":
                        asyncio.run(_run_publish(client, job))
                    else:
                        raise RuntimeError(f"未知任务类型: {job.get('type')}")
                    print(f"[agent] job {job_id} done", flush=True)
                except Exception as exc:
                    msg = str(exc)
                    print(f"[agent] job {job_id} failed: {msg}", flush=True)
                    if "已取消" not in msg:
                        try:
                            client.fail(job_id, msg)
                        except Exception as fail_exc:
                            print(f"[agent] report fail error: {fail_exc}", flush=True)
            except Exception as exc:
                print(f"[agent] loop error: {exc}", flush=True)
                time.sleep(3)
    finally:
        stop_hb.set()
        try:
            lock = _pid_lock_path()
            if lock.exists() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoSelf local agent")
    parser.add_argument("--server", default=os.environ.get("AUTOSELF_SERVER", ""))
    parser.add_argument("--token", default=os.environ.get("AUTOSELF_TOKEN", ""))
    parser.add_argument("--label", default=os.environ.get("AUTOSELF_AGENT_LABEL", "本机助手"))
    args = parser.parse_args()

    conf_dir = Path.home() / ".autoself"
    token = (args.token or "").strip()
    if not token:
        token_file = conf_dir / "token"
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()

    server = (args.server or "").strip()
    if not server:
        server_file = conf_dir / "server"
        if server_file.exists():
            server = server_file.read_text(encoding="utf-8").strip()
    if not server and (conf_dir / "config.json").exists():
        try:
            cfg = json.loads((conf_dir / "config.json").read_text(encoding="utf-8"))
            server = str(cfg.get("server") or "").strip()
            if not token:
                token = str(cfg.get("token") or "").strip()
        except Exception:
            pass
    if not server:
        server = "http://autopost.com.cn"
    # ICP/运营商会劫持部分 HTTP GET，且部分网络 HTTPS 会被重置；助手统一走 HTTP。
    if server.startswith("https://autopost.com.cn"):
        server = "http://autopost.com.cn"

    if not token:
        raise SystemExit(
            "缺少云端连接凭证。请在网页登录后重新下载「本机助手」压缩包并双击启动。"
        )
    run_forever(server, token, args.label)



if __name__ == "__main__":
    main()
