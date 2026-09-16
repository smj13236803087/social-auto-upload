"""Send AutoSelf emails via SMTP (your own mailbox)."""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from conf import SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USER


def _require_smtp() -> None:
    missing = []
    if not SMTP_HOST:
        missing.append("SMTP_HOST")
    if not SMTP_USER:
        missing.append("SMTP_USER")
    if not SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD")
    if missing:
        raise RuntimeError("邮件未配置，请设置：" + "、".join(missing))


def send_email(*, to: str, subject: str, text: str, html: str | None = None) -> None:
    _require_smtp()
    from_addr = (SMTP_FROM or SMTP_USER).strip()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    port = int(SMTP_PORT or 587)
    with smtplib.SMTP(SMTP_HOST, port, timeout=30) as server:
        server.ehlo()
        try:
            server.starttls()
            server.ehlo()
        except smtplib.SMTPException:
            pass
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(from_addr, [to], msg.as_string())


def send_verification_code_email(*, to: str, code: str, purpose: str = "register") -> None:
    purpose_text = "注册" if purpose == "register" else "验证"
    subject = f"你的{purpose_text}验证码：{code}"
    text = (
        f"你的{purpose_text}验证码是：{code}。"
        "验证码有效期 10 分钟。如非本人操作请忽略本邮件。"
    )
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,'PingFang SC','Microsoft YaHei',sans-serif;line-height:1.6">
      <h2 style="margin:0 0 12px 0;">{purpose_text}验证码</h2>
      <p style="margin:0 0 8px 0;">你的{purpose_text}验证码是：</p>
      <div style="font-size:28px;font-weight:700;letter-spacing:6px;margin:10px 0 14px 0;">{code}</div>
      <p style="margin:0;color:#666;">有效期 10 分钟。如非本人操作请忽略。</p>
    </div>
    """
    send_email(to=to, subject=subject, text=text, html=html)
