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
    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None: ...


def _check(to: str, subject: str) -> None:
    for v in (to, subject):
        if "\r" in v or "\n" in v:
            raise ValueError("header injection attempt: newline in recipient or subject")


class ConsoleMailer:
    def __init__(self) -> None:
        self.outbox: list[dict] = []

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        _check(to, subject)
        self.outbox.append({"to": to, "subject": subject, "text": text, "html": html})
        log.info("mail to=%s subject=%s (body not logged)", to, subject)


class SmtpMailer:
    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        _check(to, subject)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.s.mail_from, to, subject
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
        return ConsoleMailer()
    if settings.mail_backend == "smtp":
        if not settings.smtp_host:
            raise ValueError("MAIL_BACKEND=smtp requires SMTP_HOST")
        return SmtpMailer(settings)
    raise ValueError(f"unknown MAIL_BACKEND {settings.mail_backend!r} (use 'console' or 'smtp')")
