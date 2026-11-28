"""OTP email outcome is surfaced, never silently swallowed.

When SMTP fails, the register, resend-otp and staff two-factor responses
must carry email_sent false and must not claim an email was sent. When
SMTP accepts the message, email_sent is true and the message is the
original one. No real mail is sent: SMTP is faked at the smtplib level.
"""
import smtplib

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/m.db", "TESTING": True,
                      "UPLOAD_DIR": str(tmp_path / "up"), "EXPOSE_OTP": True})
    return app.test_client()


SMTP_CFG = {"SMTP_HOST": "mail.example.test", "SMTP_PORT": 587,
            "SMTP_USER": "noreply@portagov.giin.co.rw", "SMTP_PASS": "secret",
            "MAIL_FROM": "noreply@portagov.giin.co.rw"}


class FakeSMTP:
    """Accepts every message and records it, like a working SMTP server."""

    def __init__(self, host, port, timeout=None):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        return None

    def login(self, u, p):
        return None

    def send_message(self, msg):
        self.sent.append(msg)


class RaisingSMTP:
    """Refuses the connection, like the live SMTP failure."""

    def __init__(self, *a, **k):
        raise smtplib.SMTPException("Connection refused")


def smtp_ok(client, monkeypatch):
    from app.services import email
    client.application.config.update(SMTP_CFG)
    fake = FakeSMTP(None, None)
    monkeypatch.setattr(email.smtplib, "SMTP", lambda *a, **k: fake)
    return fake


def smtp_down(client, monkeypatch):
    from app.services import email
    client.application.config.update(SMTP_CFG)
    monkeypatch.setattr(email.smtplib, "SMTP", RaisingSMTP)


def register(client, phone="0770111222", email=None):
    body = {"full_name": "Ma Test", "phone": phone, "password": "Passw0rd!", "accept_terms": True}
    if email:
        body["email"] = email
    return client.post("/api/auth/register", json=body)


def test_register_email_sent_true(client, monkeypatch):
    fake = smtp_ok(client, monkeypatch)
    r = register(client, email="has.mail@example.com")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["email_sent"] is True
    assert body["message"] == "Account created. Enter the code sent by SMS and email."
    assert len(fake.sent) == 1
    assert fake.sent[0]["To"] == "has.mail@example.com"
    assert body["dev_otp"] in fake.sent[0].get_content()


def test_register_email_sent_false_when_smtp_raises(client, monkeypatch):
    smtp_down(client, monkeypatch)
    r = register(client, email="has.mail@example.com")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["email_sent"] is False
    assert "sent by SMS and email" not in body["message"]
    assert "The email could not be sent" in body["message"]
    assert "check your phone" in body["message"]
    # the code still exists on the server: the user can verify by SMS code
    assert body["dev_otp"]


def test_register_email_sent_false_when_smtp_not_configured(client):
    # No SMTP settings at all: nothing was sent, and the message says so.
    r = register(client, email="has.mail@example.com")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["email_sent"] is False
    assert "sent by SMS and email" not in body["message"]
    assert "The email could not be sent" in body["message"]


def test_register_email_sent_null_without_email(client, monkeypatch):
    fake = smtp_ok(client, monkeypatch)
    r = register(client)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["email_sent"] is None
    assert body["message"] == "Account created. Enter the code sent by SMS."
    assert fake.sent == []


def test_resend_otp_email_sent_false_when_smtp_raises(client, monkeypatch):
    smtp_down(client, monkeypatch)
    r = register(client, phone="0770111333", email="resend@example.com")
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/auth/resend-otp", json={"phone": "0770111333"})
    body = r.get_json()
    assert body["email_sent"] is False
    assert "Code sent" in body["message"]
    assert "sent by SMS and email" not in body["message"]
    assert "The email could not be sent" in body["message"]
    assert body["dev_otp"]


def test_resend_otp_email_sent_true(client, monkeypatch):
    smtp_ok(client, monkeypatch)
    r = register(client, phone="0770111444", email="resend@example.com")
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/auth/resend-otp", json={"phone": "0770111444"})
    body = r.get_json()
    assert body["email_sent"] is True
    assert body["message"] == "Code sent"


def test_staff_two_factor_email_sent_false_when_smtp_raises(client, monkeypatch):
    smtp_down(client, monkeypatch)
    with client.application.app_context():
        from app.extensions import db
        from app.models import User
        admin = User.query.filter_by(phone="+231770000001").first()
        admin.email = "admin@example.com"
        db.session.commit()
    r = client.post("/api/auth/login",
                    json={"identifier": "admin@example.com", "password": "Admin@12345"})
    body = r.get_json()
    assert body.get("two_factor") is True
    assert body["email_sent"] is False
    assert "sent to your phone and email" not in body["message"]
    assert "The email could not be sent" in body["message"]
    assert body["code_channels"] == ["sms", "email"]


def test_staff_two_factor_email_sent_true(client, monkeypatch):
    smtp_ok(client, monkeypatch)
    with client.application.app_context():
        from app.extensions import db
        from app.models import User
        admin = User.query.filter_by(phone="+231770000001").first()
        admin.email = "admin@example.com"
        db.session.commit()
    r = client.post("/api/auth/login",
                    json={"identifier": "admin@example.com", "password": "Admin@12345"})
    body = r.get_json()
    assert body.get("two_factor") is True
    assert body["email_sent"] is True
    assert body["message"] == "Enter the sign-in code sent to your phone and email."
