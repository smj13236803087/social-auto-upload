# Web MVP（抖音 / 快手 / B站 / 微信视频号）

中国区最小可用 Web：绑账号、贴分享链接下载/上传、博主主页日更订阅。不接 YouTube。

## 启动

```bash
cd /Applications/social-auto-upload

uv sync --extra web
# 桌面窗口（可选）：uv sync --extra desktop

# 1) 配置 MySQL（conf.py 或环境变量 AUTOSELF_MYSQL_*）
# 2) 建库建表：
mysql -u root -p < docs/autoself_schema.sql

# 首次或更新 patchright 后：
# uv run patchright install chromium

# 方式 A：终端服务 + 浏览器
uv run python -m web_mvp
# 浏览器打开 http://127.0.0.1:5410  → 先注册/登录

# 方式 B：桌面窗口 AutoSelf（自动起本地服务并打开原生窗口）
uv run autoself
```

**必须先登录**才能使用发稿 / 订阅 / 队列等功能。禁用用户：

```sql
UPDATE autoself.users SET status='disabled' WHERE email='某人@qq.com';
```

**管理后台（与普通用户入口分离，无注册）：** http://127.0.0.1:5410/admin  
默认管理员：`3360133176@qq.com` / `123456`（启动时自动确保存在）。

**日更订阅要求进程常开**（终端里的 `web_mvp` 或桌面窗口不要关）；到点由服务端后台线程触发。

## 打包桌面安装包

**Mac（在苹果电脑上）：**

```bash
cd /Applications/social-auto-upload
uv sync --extra desktop
./scripts/build_mac_desktop.sh
# 产物：dist/AutoSelf-Mac.dmg / dist/AutoSelf-Mac.pkg
```

**Windows（必须在 Windows 上）：**

```powershell
cd C:\path\to\social-auto-upload
uv sync --extra desktop
powershell -ExecutionPolicy Bypass -File .\scripts\build_win_desktop.ps1
# 产物：dist\AutoSelf-Desktop-Win.zip
# 若已安装 Inno Setup 6，额外生成：dist\AutoSelf-Win-Setup.exe
```

> 图标：`assets/icons/AutoSelf.icns`（Mac）/ `assets/icons/AutoSelf.ico`（Windows）

## 功能

1. **登录注册**：用户名 + 密码；初期免费；`status=disabled` 可立刻踢人
2. **账号**：抖音 / 快手 / B 站 / 微信视频号均在网页扫码登录（B 站不再需要终端 `sau bilibili login`）
3. **贴链接**：下载到本地，或下载后上传到已绑定账号（可多选上传账号）
4. **博主日更**：填主页链接 + 每天时间 + 目标平台账号；有新视频才下+传（按 video id 去重）。**最多 2 条订阅**
5. **本地文件夹队列**：指定文件夹 + 每天时间；按创建时间升序发未发过的视频（按文件名去重）；可预填最近 5 条的标题/文案/标签，空则默认「奔赴下一场山海」。**最多 2 条队列**
6. **上传记录**：贴链接 / 订阅 / 文件夹队列的成功、部分成功、失败、跳过都会记入列表（最多 100 条）

临时文件在 `tmp/`；订阅在 `data/creator_subscriptions.json`；文件夹队列在 `data/folder_queues.json`；上传记录在 `data/publish_history.json`。用户与会话在 MySQL 库 `autoself`。

## 验收 checklist

按顺序勾一遍即可；失败看页面「5. 上传记录」与对应区块日志。

### 0. 登录

- [ ] 能注册新用户并自动进入主界面
- [ ] 退出后再登录成功
- [ ] 未登录访问业务 API 返回 401

### A. 账号

- [ ] 抖音扫码绑定成功，列表显示真实昵称
- [ ] 快手扫码绑定成功
- [ ] B 站网页扫码登录后刷新可见
- [ ] 微信视频号扫码绑定成功，可勾选为上传目标
- [ ] 「校验 Cookie」三项均为有效（或已知失效后能「重新登录」修好）

### B. 贴链接（每个平台至少 1 条）

- [ ] 仅下载：粘贴分享链 → 能下到本地文件
- [ ] 下载并上传：勾多个目标 → 各平台成功（或记录里显示部分成功 + 失败原因）
- [ ] 含 B 站目标时分区可选，默认足球(249) 合理

### C. 博主日更

- [ ] 添加订阅（主页 + 时间 + 一个或多个目标）
- [ ] 第 3 条订阅加不上 / 提示最多 2 条
- [ ] 「立即执行」：有新视频则上传；无新视频显示跳过，记录页有对应条目
- [ ] 再点一次同一订阅 → 仍跳过（video id 去重）
- [ ] 设一个几分钟后的时间，进程保持运行，到点自动跑一次

### D. 本地文件夹队列

- [ ] 添加队列（真实文件夹 + 时间 + 一个或多个目标）
- [ ] 第 3 条队列加不上 / 提示最多 2 条
- [ ] 预填/留空标题文案后「立即发下一条」成功
- [ ] 同一文件不会重复发（文件名去重）
- [ ] 到点自动发（进程常开）

### E. 记录与稳定性

- [ ] 「5. 上传记录」能刷出上述操作
- [ ] Cookie 过期时失败信息可读，重登后可恢复

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册并自动登录 |
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前用户 |
| GET | `/api/accounts` | 账号列表；`?check=1` 校验 Cookie |
| GET | `/api/accounts/login?platform=&account=&token=` | SSE 扫码登录 |
| POST | `/api/download` | `{url}` |
| POST | `/api/download-and-publish` | `{url, targets:[{platform,account,tid?}], title?, description?, tid?}`（兼容旧字段 `platform`/`account`） |
| GET | `/api/publish-history` | `?limit=50` 上传记录 |
| GET/POST/DELETE | `/api/subscriptions` | 订阅 CRUD；POST body 含 `targets` |
| POST | `/api/subscriptions/<id>/toggle` | `{enabled}` |
| POST | `/api/subscriptions/<id>/run` | 立即执行一次 |
| GET/POST/DELETE | `/api/folder-queues` | 文件夹队列 CRUD |
| POST | `/api/folder-queues/<id>/run` | 立即发下一条 |

Cookie 路径与 CLI 一致：`cookies/{platform}_{account}.json`。
