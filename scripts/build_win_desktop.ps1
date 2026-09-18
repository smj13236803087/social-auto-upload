# 打包 Windows 桌面应用 → 绿色 ZIP（+ 可选安装包 Setup.exe）
#
# 必须在 Windows 上执行：
#   cd C:\path\to\social-auto-upload
#   powershell -ExecutionPolicy Bypass -File .\scripts\build_win_desktop.ps1
#
# 产物：
#   dist\AutoSelf\AutoSelf.exe           ← PyInstaller 目录版
#   dist\AutoSelf-Desktop-Win.zip        ← 发给用户（解压即用）
#   dist\AutoSelf-Win-Setup.exe          ← 若已安装 Inno Setup 6 则额外生成
#
# 环境变量（可选）：
#   AUTOSELF_DESKTOP_VERSION=1.0.0
#   AUTOSELF_DESKTOP_URL=https://你的站点

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if ($env:OS -notlike "*Windows*") {
  Write-Error "请在 Windows 上运行本脚本（Mac 请用 ./scripts/build_mac_desktop.sh）"
}

$Version = if ($env:AUTOSELF_DESKTOP_VERSION) { $env:AUTOSELF_DESKTOP_VERSION } else { "1.0.0" }
$DesktopUrl = if ($env:AUTOSELF_DESKTOP_URL) { $env:AUTOSELF_DESKTOP_URL.TrimEnd("/") } else { "https://autopost.com.cn" }

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
  Write-Error "缺少 .venv，先执行: uv sync --extra desktop"
}

$Icon = Join-Path $Root "assets\icons\AutoSelf.ico"
if (-not (Test-Path $Icon)) {
  Write-Error "缺少图标: assets\icons\AutoSelf.ico"
}

Write-Host "安装打包依赖…"
if (Get-Command uv -ErrorAction SilentlyContinue) {
  & uv pip install -q "pywebview>=5.3.2" "pyinstaller>=6.0" "pywin32>=306"
} else {
  & $Py -m pip install -q "pywebview>=5.3.2" "pyinstaller>=6.0" "pywin32>=306"
}

$Dist = Join-Path $Root "dist"
$Build = Join-Path $Root "build\autoself_desktop_win"
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
  (Join-Path $Dist "AutoSelf"), `
  (Join-Path $Dist "AutoSelf.exe"), `
  (Join-Path $Dist "AutoSelf-Desktop-Win.zip"), `
  (Join-Path $Dist "AutoSelf-Win-Setup.exe"), `
  (Join-Path $Dist "_win_zip"), `
  $Build

Write-Host "PyInstaller 打包中…"
& $Py -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name AutoSelf `
  --icon $Icon `
  --distpath $Dist `
  --workpath $Build `
  --specpath $Build `
  desktop_cloud.py

$ExeDir = Join-Path $Dist "AutoSelf"
$Exe = Join-Path $ExeDir "AutoSelf.exe"
if (-not (Test-Path $Exe)) {
  Write-Error "未找到 PyInstaller 产出: $Exe"
}

# 安装说明
$Readme = @"
AutoSelf $Version（Windows）

【使用】
1. 解压本压缩包到任意文件夹（不要只放在临时目录）
2. 双击 AutoSelf.exe 打开
3. 界面与网站相同，默认连接：$DesktopUrl

【改服务器地址】
用记事本新建文件（注意没有后缀名也行）：
  %USERPROFILE%\.autoself\desktop_url
内容写一行，例如：
  $DesktopUrl

【说明】
- 首次打开若被 Windows Defender / 杀毒拦截：选「仍要运行」
- 抖音相关能力仍需本机助手时，请在网页内按提示下载助手
"@
Set-Content -Path (Join-Path $ExeDir "安装说明.txt") -Value $Readme -Encoding UTF8

# ZIP
Write-Host "生成 ZIP…"
$ZipStage = Join-Path $Dist "_win_zip"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ZipStage
New-Item -ItemType Directory -Force -Path $ZipStage | Out-Null
Copy-Item -Recurse $ExeDir (Join-Path $ZipStage "AutoSelf")
$ZipPath = Join-Path $Dist "AutoSelf-Desktop-Win.zip"
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path (Join-Path $ZipStage "AutoSelf") -DestinationPath $ZipPath -Force
Remove-Item -Recurse -Force $ZipStage

# Optional Inno Setup
$Iss = Join-Path $Root "scripts\AutoSelf-Win.iss"
$Iscc = $null
foreach ($c in @(
  "${env:LocalProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)) {
  if ($c -and (Test-Path $c)) { $Iscc = $c; break }
}
if (-not $Iscc) {
  $cmd = Get-Command iscc -ErrorAction SilentlyContinue
  if ($cmd) { $Iscc = $cmd.Source }
}

if ($Iscc -and (Test-Path $Iss)) {
  Write-Host "Inno Setup 生成安装包…"
  & $Iscc `
    /DMyAppVersion=$Version `
    /DMyAppSource="$ExeDir" `
    /DMyAppIcon="$Icon" `
    /O"$Dist" `
    /F"AutoSelf-Win-Setup" `
    $Iss
} else {
  Write-Host "未检测到 Inno Setup 6，跳过 Setup.exe（ZIP 已可用）。"
  Write-Host "可选安装: https://jrsoftware.org/isinfo.php 后重跑本脚本。"
}

Write-Host ""
Write-Host "========== 发给用户（Windows）=========="
Write-Host "  推荐 ZIP：$ZipPath"
if (Test-Path (Join-Path $Dist "AutoSelf-Win-Setup.exe")) {
  Write-Host "  安装包：  dist\AutoSelf-Win-Setup.exe"
}
Get-ChildItem $ZipPath, (Join-Path $Dist "AutoSelf-Win-Setup.exe") -ErrorAction SilentlyContinue |
  ForEach-Object { "{0}`t{1:N1} MB" -f $_.FullName, ($_.Length / 1MB) }
