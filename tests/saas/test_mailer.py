import pytest

from leadengine.config import Settings
from leadengine.platform import mailer as mailer_mod
from leadengine.platform.mailer import ConsoleMailer, SmtpMailer, make_mailer


def test_console_mailer_records_message():
    m = ConsoleMailer()
    m.send("a@x.com", "Hello", "body text", "<p>body</p>")
    assert m.outbox == [{"to": "a@x.com", "subject": "Hello", "text": "body text", "html": "<p>body</p>"}]


def test_make_mailer_defaults_to_console():
    assert isinstance(make_mailer(Settings(db_path=":memory:")), ConsoleMailer)


def test_smtp_requires_host():
    with pytest.raises(ValueError, match="SMTP_HOST"):
        make_mailer(Settings(db_path=":memory:", mail_backend="smtp"))
    with pytest.raises(ValueError, match="MAIL_BACKEND"):
        make_mailer(Settings(db_path=":memory:", mail_backend="carrier-pigeon"))


@pytest.mark.parametrize("field", ["to", "subject"])
def test_header_injection_is_refused(field):
    kw = {"to": "a@x.com", "subject": "Hi", "text": "t"}
    kw[field] = kw[field] + "\r\nBcc: evil@x.com"
    for m in (ConsoleMailer(), SmtpMailer(Settings(db_path=":memory:", smtp_host="h"))):
        with pytest.raises(ValueError):
            m.send(**kw)


def test_smtp_sends_via_starttls(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            calls.append(("starttls", context is not None))

        def login(self, u, p):
            calls.append(("login", u))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"], msg["From"]))

    monkeypatch.setattr(mailer_mod.smtplib, "SMTP", FakeSMTP)
    s = Settings(db_path=":memory:", mail_backend="smtp", smtp_host="smtp.example", smtp_port=587, smtp_user="u", smtp_password="p",
                 mail_from="DigitalBurj <no-reply@example.com>")
    make_mailer(s).send("a@x.com", "Verify", "link", "<a>link</a>")
    assert calls == [("connect", "smtp.example", 587), ("starttls", True), ("login", "u"), ("send", "a@x.com", "Verify", "DigitalBurj <no-reply@example.com>")]
