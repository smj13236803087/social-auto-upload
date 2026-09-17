#!/usr/bin/env bash
# 打「可直接发给用户」的 Mac 安装包（不经过网站下载）。
#
# 用法：
#   ./scripts/build_mac_release.sh
#
# 产物：
#   dist/AutoSelf-Mac.zip
# 你把这个 zip 放到网盘 / 官网 / 微信，用户下载解压即可。
#
# 用户首次打开会输入：服务器地址 + 邮箱 + 密码（自动登录拿 token）。
# 当前版本仍需用户本机有一份已安装依赖的 social-auto-upload 项目
#（或把项目放在 zip 同级目录 social-auto-upload/ 一并分发）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_SRC="$ROOT/web_mvp/static/desktop/mac/AutoSelf.app"
OUT_DIR="$ROOT/dist"
STAGE="$OUT_DIR/_release_mac"
ZIP="$OUT_DIR/AutoSelf-Mac.zip"
VER="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP_SRC/Contents/Info.plist" 2>/dev/null || echo 0.2.0)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "请在苹果电脑上打包。" >&2
  exit 1
fi

chmod +x "$APP_SRC/Contents/MacOS/AutoSelf"
xattr -cr "$APP_SRC" 2>/dev/null || true
/usr/bin/plutil -lint "$APP_SRC/Contents/Info.plist"

rm -rf "$STAGE" "$ZIP"
mkdir -p "$STAGE/AutoSelf-Mac"

ditto "$APP_SRC" "$STAGE/AutoSelf-Mac/AutoSelf助手.app"
xattr -cr "$STAGE/AutoSelf-Mac/AutoSelf助手.app" 2>/dev/null || true

cat >"$STAGE/AutoSelf-Mac/给用户的说明.txt" <<EOF
AutoSelf 本机助手（Mac）v${VER}
================================

【你怎么用】
1. 解压本压缩包
2. 双击「AutoSelf助手」
   （若提示无法打开：右键图标 → 打开）
3. 首次会询问：
   - 服务器地址（例：http://82.156.164.185 或你的已备案域名）
   - 登录邮箱、密码（管理员给你开的账号）
4. 右上角出现「已在后台运行」即可
5. 然后用浏览器打开同样的服务器地址，登录网页做发布/订阅

【本机还需要】
助手要调用本机 Python 项目。任选其一：
A. 把「social-auto-upload」项目文件夹放到和「AutoSelf助手」同一层，并已执行：
     uv sync --extra web
   （开发者若做成完整分发包，会直接带上这个文件夹）
B. 或本机已有该项目；首次启动时在弹窗里选中项目文件夹

日志：~/Library/Logs/AutoSelf-agent.log
EOF

cat >"$STAGE/AutoSelf-Mac/开发者分发说明.txt" <<EOF
把整个「AutoSelf-Mac」文件夹打成 zip 发给用户即可。

推荐完整分发（用户零配置项目路径）：
  1. 在本机准备好已 uv sync --extra web 的 social-auto-upload
  2. 复制到 AutoSelf-Mac/social-auto-upload/（不要带 .git、cookies、data 里的隐私）
  3. 再压缩发给用户

命令示例：
  rsync -a --exclude .git --exclude cookies --exclude data --exclude logs \\
    /path/to/social-auto-upload/ AutoSelf-Mac/social-auto-upload/
EOF

mkdir -p "$OUT_DIR"
(
  cd "$STAGE"
  export COPYFILE_DISABLE=1
  /usr/bin/zip -ry "$ZIP" AutoSelf-Mac
)
rm -rf "$STAGE"

echo "OK → $ZIP"
echo "发给用户：把这个 zip 上传到网盘/官网，对方下载解压双击即可。"
ls -lh "$ZIP"
