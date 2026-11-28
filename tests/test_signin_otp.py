"""Sign-in OTP goes to both channels the account has, once each.

The login flow sends the staff sign-in code by SMS and by email (real
SMTP) when the account has an email. Before this fix, an account with
an email got the code by SMS twice, because notify also queued an SMS
for channel email notifications. See frontend issue #19.

EXPOSE_OTP stays on: these tests read the sent calls, not the code
shown on screen, and nothing here flips the setting off.
"""

from unittest import mock

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/t.db", "TESTING": True,
                      "UPLOAD_DIR": str(tmp_path / "up"), "EXPOSE_OTP": True})
    return app.test_client()


def _set_admin_email(client, email):
    with client.application.app_context():
        from app.extensions import db
        from app.models import User
        admin = User.query.filter_by(phone="+231770000001").first()
        admin.email = email
        db.session.commit()


def _staff_login(client, identifier):
    with mock.patch("app.services.sms.send") as sms_send, mock.patch("app.services.email.send") as email_send:
        r = client.post("/api/auth/login", json={"identifier": identifier, "password": "Admin@12345"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body.get("two_factor") is True
        return body, sms_send, email_send


def test_staff_login_otp_goes_to_both_channels_once(client):
    _set_admin_email(client, "admin@example.com")
    body, sms_send, email_send = _staff_login(client, "admin@example.com")
    assert body.get("code_channels") == ["sms", "email"]
    assert sms_send.call_count == 1
    assert email_send.call_count == 1
    assert email_send.call_args[0][0] == "admin@example.com"


def test_staff_login_otp_phone_only_without_email(client):
    _set_admin_email(client, None)
    body, sms_send, email_send = _staff_login(client, "+231770000001")
    assert body.get("code_channels") == ["sms"]
    assert sms_send.call_count == 1
    assert email_send.call_count == 0
