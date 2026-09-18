#!/usr/bin/env bash
# Windows 打包入口提示（真正打包请在 Windows 上跑 PowerShell 脚本）
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\build_win_desktop.ps1
#
set -euo pipefail
cat <<'EOF'
请在 Windows 电脑上打包（不能在 Mac 上直接产出 .exe）：

  cd <social-auto-upload 仓库>
  uv sync --extra desktop
  powershell -ExecutionPolicy Bypass -File .\scripts\build_win_desktop.ps1

产物：
  dist\AutoSelf-Desktop-Win.zip     ← 推荐发给用户
  dist\AutoSelf-Win-Setup.exe       ← 若安装了 Inno Setup 6

Mac 打包仍用：
  ./scripts/build_mac_desktop.sh
EOF
exit 1
