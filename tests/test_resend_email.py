"""Outbound mail through the Resend HTTPS API.

When RESEND_API_KEY is set, send() uses Resend instead of SMTP, because
outbound SMTP from Render free to StackMail times out. When the key is
absent, the old SMTP path is used unchanged. No real mail is sent: the
Resend HTTP call is faked at the urllib level and SMTP is faked too.
"""
import json
import smtplib
import urllib.error
import urllib.request

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/m.db", "TESTING": True,
                      "UPLOAD_DIR": str(tmp_path / "up"), "EXPOSE_OTP": True})
    return app.test_client()


RESEND_CFG = {"RESEND_API_KEY": "re_test_key", "MAIL_FROM": "portagov@giin.co.rw"}
SMTP_CFG = {"SMTP_HOST": "mail.example.test", "SMTP_PORT": 587,
            "SMTP_USER": "noreply@portagov.giin.co.rw", "SMTP_PASS": "secret",
            "MAIL_FROM": "noreply@portagov.giin.co.rw"}


class FakeResp:
    """A successful Resend reply (HTTP 200 with an id)."""

    def __init__(self):
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"id": "re_test_id"}'


def resend_ok(client, monkeypatch):
    from app.services import email
    client.application.config.update(RESEND_CFG)
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return FakeResp()
    monkeypatch.setattr(email.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_send_via_resend_success(client, monkeypatch):
    from app.services import email
    calls = resend_ok(client, monkeypatch)
    with client.application.app_context():
        assert email.send("user@example.com", "Subject", "Body text") is True
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://api.resend.com/emails"
    assert req.get_header("Authorization") == "Bearer re_test_key"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["from"] == "portagov@giin.co.rw"
    assert payload["to"] == ["user@example.com"]
    assert payload["subject"] == "Subject"
    assert payload["text"] == "Body text"


def test_send_via_resend_http_error_returns_false(client, monkeypatch):
    from app.services import email
    client.application.config.update(RESEND_CFG)
    def raising(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(email.urllib.request, "urlopen", raising)
    with client.application.app_context():
        assert email.send("user@example.com", "Subject", "Body") is False


def test_send_via_resend_network_error_returns_false(client, monkeypatch):
    from app.services import email
    client.application.config.update(RESEND_CFG)
    def raising(req, timeout=None):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(email.urllib.request, "urlopen", raising)
    with client.application.app_context():
        assert email.send("user@example.com", "Subject", "Body") is False


def test_resend_is_preferred_over_smtp(client, monkeypatch):
    from app.services import email
    client.application.config.update({**SMTP_CFG, **RESEND_CFG})
    calls = resend_ok(client, monkeypatch)
    def bad_smtp(*a, **k):
        raise AssertionError("SMTP must not be used when a Resend key is set")
    monkeypatch.setattr(email.smtplib, "SMTP", bad_smtp)
    with client.application.app_context():
        assert email.send("user@example.com", "Subject", "Body") is True
    assert len(calls) == 1


def test_no_resend_key_falls_back_to_smtp(client, monkeypatch):
    from app.services import email
    client.application.config.update(SMTP_CFG)
    sent = []
    class FakeSMTP:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self):
            return None
        def login(self, u, p):
            return None
        def send_message(self, msg):
            sent.append(msg)
    monkeypatch.setattr(email.smtplib, "SMTP", FakeSMTP)
    with client.application.app_context():
        assert email.send("user@example.com", "Subject", "Body") is True
    assert len(sent) == 1


def test_status_reports_provider(client):
    from app.services import email
    with client.application.app_context():
        assert email.status()["provider"] == "smtp"
        assert email.status()["configured"] is False
    client.application.config.update(RESEND_CFG)
    with client.application.app_context():
        assert email.status() == {"configured": True, "provider": "resend",
                                  "missing": [], "from": "portagov@giin.co.rw"}


def test_register_otp_email_goes_via_resend(client, monkeypatch):
    calls = resend_ok(client, monkeypatch)
    r = client.post("/api/auth/register", json={"full_name": "Ma Test", "phone": "0770111444",
                                                "email": "has.mail@example.com", "password": "Passw0rd!",
                                                "accept_terms": True})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["email_sent"] is True
    assert body["message"] == "Account created. Enter the code sent by SMS and email."
    assert len(calls) == 1
    payload = json.loads(calls[0].data.decode("utf-8"))
    assert payload["to"] == ["has.mail@example.com"]
    assert body["dev_otp"] in payload["text"]


def test_register_otp_email_false_when_resend_fails(client, monkeypatch):
    from app.services import email
    client.application.config.update(RESEND_CFG)
    def raising(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(email.urllib.request, "urlopen", raising)
    r = client.post("/api/auth/register", json={"full_name": "Ma Test", "phone": "0770111555",
                                                "email": "has.mail@example.com", "password": "Passw0rd!",
                                                "accept_terms": True})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["email_sent"] is False
    assert "The email could not be sent" in body["message"]
    assert body["dev_otp"]
