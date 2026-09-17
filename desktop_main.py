"""Desktop shell: start local web_mvp and open it in a native window."""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5410
WINDOW_TITLE = "AutoSelf"


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def _wait_ready(url: str, *, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if 200 <= getattr(resp, "status", 200) < 500:
                    return
        except Exception as exc:  # noqa: BLE001 - probe until ready
            last_err = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"本地服务启动超时：{url}" + (f"（{last_err}）" if last_err else ""))


def _start_backend(host: str, port: int) -> None:
    from web_mvp.app import run_server

    run_server(host=host, port=port, debug=False)


def main() -> None:
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    url = f"http://{host}:{port}/"

    already = _port_open(host, port)
    if not already:
        thread = threading.Thread(
            target=_start_backend,
            args=(host, port),
            name="sau-web-mvp",
            daemon=True,
        )
        thread.start()
        _wait_ready(url)
    else:
        print(f"检测到 {url} 已在运行，直接打开窗口。", flush=True)

    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "缺少桌面依赖。请先执行：\n"
            "  uv sync --extra desktop\n"
            "然后：\n"
            "  uv run autoself"
        ) from exc

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=1200,
        height=900,
        min_size=(900, 640),
    )
    webview.start()


def _gui_alert(message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        import subprocess

        subprocess.run(
            ["osascript", "-e", f'display alert "AutoSelf" message "{message}" as critical'],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"AutoSelf 启动失败: {exc}", flush=True)
        _gui_alert(f"启动失败：{exc}")
        sys.exit(1)
