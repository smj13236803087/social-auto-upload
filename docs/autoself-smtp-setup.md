# 申请自己的发信邮箱（QQ 邮箱 SMTP）

后台 `mini-program-backstage` 用的是 **QQ 邮箱 SMTP**。你不要用别人的 `SMTP_USER` / `SMTP_PASS`，按下面用**自己的 QQ 号**申请一份授权码。

## 1. 准备一个 QQ 邮箱

1. 打开 [QQ 邮箱](https://mail.qq.com/) 并用你的 QQ 登录  
2. 建议单独用一个工作号（例如以后给用户发验证码的专用邮箱）

## 2. 开启 SMTP 并生成「授权码」

1. 登录网页版 QQ 邮箱  
2. 右上角 **设置** → **账户**  
3. 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务**  
4. 开启 **IMAP/SMTP** 或 **POP3/SMTP**（按页面提示）  
5. 按提示用手机发短信验证  
6. 生成 **授权码**（一串字母，**不是** QQ 登录密码）  
7. 复制保存好（只显示一次）

> 授权码泄露等于别人能用你的邮箱发信，不要提交到 Git、不要发给别人。

## 3. 填进 AutoSelf

编辑 `conf.py`（或环境变量）：

```python
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587
SMTP_USER = "你的QQ号@qq.com"
SMTP_PASSWORD = "刚才生成的授权码"
SMTP_FROM = "AutoSelf <你的QQ号@qq.com>"  # 可选
```

或：

```bash
export AUTOSELF_SMTP_USER='你的QQ号@qq.com'
export AUTOSELF_SMTP_PASSWORD='授权码'
```

## 4. 验证

重启服务后，在 AutoSelf 注册页点「获取验证码」，应能在目标邮箱收到 6 位数。

若失败，常见原因：

- 填了登录密码而不是授权码  
- `SMTP_USER` 不是完整邮箱  
- QQ 邮箱未开启 SMTP  
- 本机网络拦了 587 端口（可试改 `SMTP_PORT = 465`，需要的话再说，我帮你改代码）

## 其他邮箱（可选）

| 邮箱 | SMTP_HOST | 说明 |
|------|-----------|------|
| QQ | `smtp.qq.com` | 国内最省事，与 backstage 一致 |
| 163 | `smtp.163.com` | 同样要开 SMTP 授权码 |
| Gmail | `smtp.gmail.com` | 需 Google 应用专用密码，国内可能不稳 |

## 和 backstage 的关系

流程对齐：注册 → 发 6 位码 → 校验入库 → 自动登录。  
**发信账号必须是你自己的**；不要复制 backstage 的 `.env` 里别人的 `SMTP_*`。
