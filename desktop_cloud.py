"""AutoSelf 桌面壳：窗口里打开和网页一模一样的界面（连你的云端）。"""

from __future__ import annotations

import os
import sys

WINDOW_TITLE = "AutoSelf"
# 备案前默认用 IP；可被环境变量 / 旁路配置覆盖
DEFAULT_URL = os.environ.get("AUTOSELF_DESKTOP_URL", "http://82.156.164.185").rstrip("/")


def _load_saved_url() -> str:
    cfg = os.path.expanduser("~/.autoself/desktop_url")
    try:
        if os.path.isfile(cfg):
            raw = open(cfg, encoding="utf-8").read().strip().rstrip("/")
            if raw:
                return raw
    except OSError:
        pass
    return DEFAULT_URL


def _gui_alert(message: str) -> None:
    if sys.platform != "darwin":
        print(message, flush=True)
        return
    try:
        import subprocess

        safe = message.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display alert "AutoSelf" message "{safe}" as critical'],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def main() -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "缺少 pywebview。请执行：uv sync --extra desktop\n然后：uv run python desktop_cloud.py"
        ) from exc

    url = _load_saved_url()
    if "://" not in url:
        url = "http://" + url

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=1280,
        height=900,
        min_size=(900, 640),
    )
    webview.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"AutoSelf 启动失败: {exc}", flush=True)
        _gui_alert(f"启动失败：{exc}")
        sys.exit(1)
