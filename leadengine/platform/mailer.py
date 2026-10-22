"""Outbound email. `ConsoleMailer` (default) records messages so dev and tests can read verification/reset links."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from ..config import Settings

log = logging.getLogger("leadengine.mail")


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str, html: str | None = None, headers: dict | None = None) -> None: ...


def _check(to: str, subject: str) -> None:
    for v in (to, subject):
        if "\r" in v or "\n" in v:
            raise ValueError("header injection attempt: newline in recipient or subject")


class ConsoleMailer:
    """Development/test mailer: keeps the last 200 messages in `outbox`. The body (it holds sign-in and reset links) is logged only when
    `log_body` is set, which `make_mailer` does for DEV_INSECURE_COOKIES=1 deployments."""

    def __init__(self, log_body: bool = False) -> None:
        self.outbox: list[dict] = []
        self.log_body = log_body

    def send(self, to: str, subject: str, text: str, html: str | None = None, headers: dict | None = None) -> None:
        _check(to, subject)
        self.outbox.append({"to": to, "subject": subject, "text": text, "html": html, **({"headers": dict(headers)} if headers else {})})
        del self.outbox[:-200]
        if self.log_body:
            log.warning("DEV MAIL to=%s subject=%s\n%s", to, subject, text)
        else:
            log.info("mail to=%s subject=%s (body not logged)", to, subject)


class SmtpMailer:
    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, to: str, subject: str, text: str, html: str | None = None, headers: dict | None = None) -> None:
        _check(to, subject)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.s.mail_from, to, subject
        for k, v in (headers or {}).items():
            _check(k, v)
            msg[k] = v
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)


def make_mailer(settings: Settings) -> Mailer:
    if settings.mail_backend == "console":
        return ConsoleMailer(log_body=settings.dev_insecure_cookies)
    if settings.mail_backend == "smtp":
        if not settings.smtp_host:
            raise ValueError("MAIL_BACKEND=smtp requires SMTP_HOST")
        return SmtpMailer(settings)
    raise ValueError(f"unknown MAIL_BACKEND {settings.mail_backend!r} (use 'console' or 'smtp')")
