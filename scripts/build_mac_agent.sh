#!/usr/bin/env bash
# 打包 Mac「AutoSelf本机助手」.app 模板，供网页「下载本机助手」使用。
#
# 用法（在 Mac 上）：
#   cd /path/to/social-auto-upload
#   ./scripts/build_mac_agent.sh
#
# 产物：
#   dist/AutoSelf助手-mac-template.zip   ← 可发给自己测试（无登录 token）
#   web_mvp/static/desktop/mac/AutoSelf.app  ← 已校验可执行；部署后用户登录网页下载才带 token
#
# 用户真正使用时：登录 https 站点 → 点「下载本机助手」→ 得到带个人凭证的 zip。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/web_mvp/static/desktop/mac/AutoSelf.app"
OUT_DIR="$ROOT/dist"
ZIP="$OUT_DIR/AutoSelf助手-mac-template.zip"
STAGE="$OUT_DIR/_mac_agent_stage"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "请在苹果电脑上运行本脚本（需要生成 .app）。" >&2
  exit 1
fi

if [[ ! -f "$APP/Contents/MacOS/AutoSelf" ]]; then
  echo "缺少 $APP" >&2
  exit 1
fi

chmod +x "$APP/Contents/MacOS/AutoSelf"
# 清掉本机隔离标记，避免打进 zip 后别人打不开
xattr -cr "$APP" 2>/dev/null || true

# 校验 shebang / Info.plist
/usr/bin/plutil -lint "$APP/Contents/Info.plist"
head -1 "$APP/Contents/MacOS/AutoSelf" | grep -q bash

mkdir -p "$OUT_DIR"
rm -rf "$STAGE" "$ZIP"
mkdir -p "$STAGE/autoself-agent"

# 复制为中文显示名（用户下载包里也是这个名字）
ditto "$APP" "$STAGE/autoself-agent/AutoSelf助手.app"
cat >"$STAGE/autoself-agent/说明.txt" <<'EOF'
【AutoSelf 本机助手 · Mac】

给最终用户（推荐）：
  1. 打开网站并登录
  2. 点击「下载本机助手」得到带登录凭证的 zip
  3. 解压后只双击「AutoSelf助手」
  4. 若提示损坏/无法打开：右键 → 打开；或终端执行：
       xattr -cr "/路径/AutoSelf助手.app"
  5. 看到右上角通知后，回网页点「我已启动，重新检测」

本机还需一次开发环境（助手会调用项目里的 Python）：
  - 已克隆 social-auto-upload
  - 已创建 .venv 并安装依赖（uv sync --extra web）
  首次启动若找不到项目，会弹窗让你选项目文件夹。

本 zip 是「模板」，不含个人 token；正式给别人用请让对方从网页下载。
EOF

# zip（保留可执行位，去掉 __MACOSX 垃圾）
rm -f "$ZIP"
(
  cd "$STAGE"
  export COPYFILE_DISABLE=1
  /usr/bin/zip -ry "$ZIP" autoself-agent
)

rm -rf "$STAGE"
echo "OK: $ZIP"
echo "下一步："
echo "  1) 把更新后的 web_mvp/static/desktop/mac/ 部署到服务器"
echo "  2) 用户登录网站 → 下载本机助手（自动带上他的 token）"
ls -lh "$ZIP"
