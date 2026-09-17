#!/usr/bin/env bash
# 打包 Mac 桌面应用 → 系统级安装包（DMG + PKG）
#
#   ./scripts/build_mac_desktop.sh
#
# 产物（发给用户优先用这两个）：
#   dist/AutoSelf-Mac.dmg     ← 双击打开，拖到「应用程序」
#   dist/AutoSelf-Mac.pkg     ← 双击按向导安装到 /Applications
#   dist/AutoSelf.app         ← 构建中间产物
#   dist/AutoSelf-Desktop-Mac.zip  ← 备用绿色版
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="${AUTOSELF_DESKTOP_VERSION:-1.0.0}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "请在苹果电脑上打包" >&2
  exit 1
fi

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "缺少 .venv，先执行: uv sync --extra desktop" >&2
  exit 1
fi

echo "安装打包依赖…"
if command -v uv >/dev/null 2>&1; then
  uv pip install -q "pywebview>=5.3.2" "pyinstaller>=6.0"
else
  "$PY" -m pip install -q "pywebview>=5.3.2" "pyinstaller>=6.0"
fi

rm -rf build/autoself_desktop \
  dist/AutoSelf dist/AutoSelf.app \
  dist/AutoSelf-Desktop-Mac.zip \
  dist/AutoSelf-Mac.dmg \
  dist/AutoSelf-Mac.pkg \
  dist/_dmg dist/_pkgroot dist/_desktop_zip
mkdir -p dist

echo "PyInstaller 打包中…"
"$PY" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name AutoSelf \
  --icon "$ROOT/assets/icons/AutoSelf.icns" \
  --osx-bundle-identifier cn.autopost.autoself \
  --distpath dist \
  --workpath build/autoself_desktop \
  --specpath build/autoself_desktop \
  desktop_cloud.py

if [[ ! -d dist/AutoSelf.app ]]; then
  if [[ -d dist/AutoSelf ]]; then
    mkdir -p dist/AutoSelf.app/Contents/MacOS dist/AutoSelf.app/Contents/Resources
    cat >dist/AutoSelf.app/Contents/Info.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>AutoSelf</string>
  <key>CFBundleIdentifier</key><string>cn.autopost.autoself</string>
  <key>CFBundleName</key><string>AutoSelf</string>
  <key>CFBundleDisplayName</key><string>AutoSelf</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
    rm -rf dist/AutoSelf.app/Contents/MacOS
    mv dist/AutoSelf dist/AutoSelf.app/Contents/MacOS
    chmod +x dist/AutoSelf.app/Contents/MacOS/AutoSelf 2>/dev/null || true
  else
    echo "未找到 PyInstaller 产出" >&2
    ls -la dist >&2
    exit 1
  fi
fi

# 写入版本号（若 PyInstaller 已生成 Info.plist）
if [[ -f dist/AutoSelf.app/Contents/Info.plist ]]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" dist/AutoSelf.app/Contents/Info.plist 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string ${VERSION}" dist/AutoSelf.app/Contents/Info.plist
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${VERSION}" dist/AutoSelf.app/Contents/Info.plist 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${VERSION}" dist/AutoSelf.app/Contents/Info.plist
fi

xattr -cr dist/AutoSelf.app 2>/dev/null || true

# 确保系统图标生效
if [[ -f assets/icons/AutoSelf.icns ]]; then
  mkdir -p dist/AutoSelf.app/Contents/Resources
  cp assets/icons/AutoSelf.icns dist/AutoSelf.app/Contents/Resources/AutoSelf.icns
  cp assets/icons/AutoSelf.icns dist/AutoSelf.app/Contents/Resources/icon-windowed.icns
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AutoSelf" dist/AutoSelf.app/Contents/Info.plist 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AutoSelf" dist/AutoSelf.app/Contents/Info.plist
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconName AutoSelf" dist/AutoSelf.app/Contents/Info.plist 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleIconName string AutoSelf" dist/AutoSelf.app/Contents/Info.plist
fi
touch dist/AutoSelf.app

# ---------- 系统级：PKG（安装到 /Applications）----------
echo "生成 PKG 安装包…"
mkdir -p dist/_pkgroot
ditto dist/AutoSelf.app dist/_pkgroot/AutoSelf.app
# 安装后清隔离属性，减少「已损坏」误报
mkdir -p dist/_pkgscripts
cat >dist/_pkgscripts/postinstall <<'EOS'
#!/bin/bash
xattr -cr "/Applications/AutoSelf.app" 2>/dev/null || true
exit 0
EOS
chmod +x dist/_pkgscripts/postinstall

pkgbuild \
  --root dist/_pkgroot \
  --install-location /Applications \
  --scripts dist/_pkgscripts \
  --identifier cn.autopost.autoself \
  --version "$VERSION" \
  dist/AutoSelf-Mac.pkg

# ---------- 系统级：DMG（拖到应用程序）----------
echo "生成 DMG…"
mkdir -p dist/_dmg
ditto dist/AutoSelf.app "dist/_dmg/AutoSelf.app"
ln -s /Applications "dist/_dmg/Applications"
cat >"dist/_dmg/安装说明.txt" <<EOF
AutoSelf ${VERSION}（Mac）

【安装】
1. 把「AutoSelf」拖到旁边的「Applications（应用程序）」文件夹
2. 打开「启动台」或「应用程序」里的 AutoSelf
3. 若提示无法打开：右键图标 → 打开

【说明】
界面与网站相同。默认连接 http://82.156.164.185
改服务器：echo 'http://你的地址' > ~/.autoself/desktop_url
EOF

hdiutil create \
  -volname "安装 AutoSelf" \
  -srcfolder dist/_dmg \
  -ov \
  -format UDZO \
  dist/AutoSelf-Mac.dmg

# ---------- 备用 zip ----------
STAGE=dist/_desktop_zip
mkdir -p "$STAGE"
ditto dist/AutoSelf.app "$STAGE/AutoSelf.app"
cp "dist/_dmg/安装说明.txt" "$STAGE/安装说明.txt"
(
  cd "$STAGE"
  export COPYFILE_DISABLE=1
  zip -ry ../AutoSelf-Desktop-Mac.zip AutoSelf.app 安装说明.txt
)

rm -rf dist/_dmg dist/_pkgroot dist/_pkgscripts dist/_desktop_zip

echo ""
echo "========== 发给用户（系统级安装）=========="
echo "  推荐 DMG：dist/AutoSelf-Mac.dmg   （拖进应用程序）"
echo "  或用 PKG：dist/AutoSelf-Mac.pkg   （安装向导 → /Applications）"
echo "  备用 ZIP：dist/AutoSelf-Desktop-Mac.zip"
ls -lh dist/AutoSelf-Mac.dmg dist/AutoSelf-Mac.pkg dist/AutoSelf-Desktop-Mac.zip 2>/dev/null
