from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
XHS_SERVER = "http://127.0.0.1:11901"  # only used by xhs-related flows
LOCAL_CHROME_PATH = ""  # optional, e.g. C:/Program Files/Google/Chrome/Application/chrome.exe
LOCAL_CHROME_HEADLESS = True  # default headless behavior for uploader/examples
DEBUG_MODE = True  # default debug behavior
# Optional proxy for the YouTube uploader. Where youtube.com is blocked, direct
# connections time out and the (patchright) chromium does NOT use the system proxy.
# Point this at your local proxy port, e.g. "http://127.0.0.1:7890". None = no proxy.
YT_PROXY = None

# AutoSelf auth (MySQL). Override via env: AUTOSELF_MYSQL_HOST / USER / PASSWORD / DB / PORT
MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 3306
MYSQL_USER = "root"
MYSQL_PASSWORD = ""
MYSQL_DATABASE = "autoself"
AUTH_TOKEN_DAYS = 30

# SMTP — 用你自己的邮箱（不要抄别人的授权码）。申请步骤见 docs/autoself-smtp-setup.md
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587
SMTP_USER = ""       # 完整邮箱，例如 123456@qq.com（不是纯 QQ 号）
SMTP_PASSWORD = ""   # QQ 邮箱「授权码」，不是登录密码
SMTP_FROM = ""       # 可选；空则等于 SMTP_USER。也可写：AutoSelf <123456@qq.com>
