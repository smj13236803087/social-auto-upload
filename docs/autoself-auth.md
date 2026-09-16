# AutoSelf 用户体系（邮箱注册）

流程对齐 mini-program-backstage：邮箱 + 密码 → 发 6 位验证码 → 校验后建号并登录。

发信必须用**你自己的** SMTP，见 [autoself-smtp-setup.md](./autoself-smtp-setup.md)。

## 建库

新库：

```bash
mysql -u root -p < docs/autoself_schema.sql
```

若已建过旧版（仅用户名）：

```bash
mysql -u root -p < docs/autoself_schema_email_migrate.sql
```

并确保 `users.email` 对老数据补齐或清空后重注册。

## 配置

`conf.py` / 环境变量：

- MySQL：`AUTOSELF_MYSQL_*`
- SMTP：`AUTOSELF_SMTP_HOST/PORT/USER/PASSWORD/FROM`

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register/request-code` | `{email,password,username?}` 发验证码 |
| POST | `/api/auth/register/verify` | `{email,code}` 完成注册并登录 |
| POST | `/api/auth/login` | `{email,password}` |
| POST | `/api/auth/logout` | Bearer |
| GET | `/api/auth/me` | 当前用户 |

## 禁用用户

```sql
UPDATE autoself.users SET status='disabled' WHERE email='某人@qq.com';
```
