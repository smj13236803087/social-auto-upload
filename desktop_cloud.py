"""AutoSelf 桌面壳：窗口里打开云端网页；拦截误跳转；提供返回与另存为。"""

from __future__ import annotations

import base64
import os
import sys
import threading
import time


WINDOW_TITLE = "AutoSelf"
DEFAULT_URL = os.environ.get("AUTOSELF_DESKTOP_URL", "http://82.156.164.185").rstrip("/")
MEDIA_EXTS = (".mp4", ".webm", ".mov", ".m4v", ".mp3", ".m4a", ".aac", ".wav")


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


def _is_media_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    if u.startswith("blob:") or u.startswith("data:"):
        return True
    path = u.split("?", 1)[0].split("#", 1)[0]
    return any(path.endswith(ext) for ext in MEDIA_EXTS)


def main() -> None:
    try:
        import webview
        from webview import menu as webview_menu
    except ImportError as exc:
        raise SystemExit(
            "缺少 pywebview。请执行：uv sync --extra desktop\n然后：uv run python desktop_cloud.py"
        ) from exc

    home = _load_saved_url()
    if "://" not in home:
        home = "http://" + home
    home = home.rstrip("/")

    def go_home() -> bool:
        if webview.windows:
            webview.windows[0].load_url(home + "/")
        return True

    class Api:
        def go_home(self) -> bool:
            return go_home()

        def save_file(self, filename: str, b64_data: str) -> bool:
            """Save base64 file via native dialog (avoids blob navigation in WKWebView)."""
            if not webview.windows:
                return False
            window = webview.windows[0]
            safe_name = (filename or "video.mp4").replace("/", "_").replace("\\", "_")
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=os.path.expanduser("~/Downloads"),
                save_filename=safe_name,
            )
            if not result:
                return False
            path = result if isinstance(result, str) else result[0]
            raw = base64.b64decode(b64_data)
            with open(path, "wb") as f:
                f.write(raw)
            return True

    api = Api()
    window = webview.create_window(
        WINDOW_TITLE,
        home + "/",
        js_api=api,
        width=1280,
        height=900,
        min_size=(900, 640),
    )

    # Always-visible floating back button (works on normal HTML pages).
    inject_js = f"""
    (function () {{
      var home = {home!r};
      var existing = document.getElementById('autoself-back-fab');
      if (!existing) {{
        var btn = document.createElement('button');
        btn.id = 'autoself-back-fab';
        btn.type = 'button';
        btn.textContent = '← 返回首页';
        btn.setAttribute('style', [
          'position:fixed', 'top:12px', 'left:12px', 'z-index:2147483647',
          'padding:8px 14px', 'border:none', 'border-radius:999px',
          'background:#1d4ed8', 'color:#fff', 'font:600 13px/1.2 -apple-system,sans-serif',
          'box-shadow:0 6px 18px rgba(0,0,0,.35)', 'cursor:pointer'
        ].join(';'));
        btn.onclick = function () {{
          try {{
            if (window.pywebview && window.pywebview.api && window.pywebview.api.go_home) {{
              window.pywebview.api.go_home();
              return;
            }}
          }} catch (e) {{}}
          location.href = home + '/';
        }};
        (document.body || document.documentElement).appendChild(btn);
        existing = btn;
      }}
      try {{
        var path = location.pathname || '/';
        var onlyVideo = !!document.querySelector('video') && !document.getElementById('appMain');
        var isAppHome = (path === '/' || path === '') && document.getElementById('appMain') && !onlyVideo;
        existing.style.display = isAppHome ? 'none' : 'block';
      }} catch (e) {{}}
    }})();
    """

    def _on_loaded():
        try:
            window.evaluate_js(inject_js)
        except Exception:
            pass

    def _on_navigating(url: str):
        # Block blob/media navigations that steal the whole window.
        if _is_media_url(url):
            return False
        return True

    try:
        window.events.loaded += _on_loaded
    except Exception:
        pass
    try:
        window.events.navigating += _on_navigating
    except Exception:
        pass

    def _watchdog():
        """If WebView still lands on a media document, bounce home; else keep FAB injected."""
        while True:
            time.sleep(0.6)
            try:
                if not webview.windows:
                    continue
                w = webview.windows[0]
                cur = ""
                try:
                    cur = w.get_current_url() or ""
                except Exception:
                    cur = ""
                if _is_media_url(cur):
                    go_home()
                    continue
                try:
                    w.evaluate_js(inject_js)
                except Exception:
                    pass
            except Exception:
                pass

    threading.Thread(target=_watchdog, name="autoself-watchdog", daemon=True).start()

    menus = [
        webview_menu.Menu(
            "导航",
            [
                webview_menu.MenuAction("返回首页", go_home),
            ],
        )
    ]
    webview.start(menu=menus)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"AutoSelf 启动失败: {exc}", flush=True)
        _gui_alert(f"启动失败：{exc}")
        sys.exit(1)
