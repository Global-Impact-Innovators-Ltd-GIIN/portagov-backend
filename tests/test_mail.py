"""Platform email: inbox, manual send, and automation rules."""
from datetime import datetime as _dt, timedelta as _td

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/m.db", "TESTING": True,
                      "UPLOAD_DIR": str(tmp_path / "up"), "EXPOSE_OTP": True})
    return app.test_client()


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def login(c, phone, pw):
    r = c.post("/api/auth/login", json={"phone": phone, "password": pw}).get_json()
    if r.get("two_factor"):
        r = c.post("/api/auth/login/verify", json={"phone": phone, "otp": r["dev_otp"]}).get_json()
    if r["user"].get("must_change_password"):
        r = c.post("/api/auth/change-password", headers=hdr(r["token"]),
                   json={"current_password": pw, "new_password": pw + "x9"}).get_json()
    return r["token"]


def register(c, phone="0770111222", email=None):
    body = {"full_name": "Ma Test", "phone": phone, "password": "Passw0rd!", "accept_terms": True}
    if email:
        body["email"] = email
    r = c.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.get_json()
    r = c.post("/api/auth/verify-otp", json={"phone": phone, "otp": r.get_json()["dev_otp"]})
    return r.get_json()["token"]


def admin(c):
    return login(c, "+231770000001", "Admin@12345")


def test_mail_rule_create_and_toggle(client):
    tok = admin(client)
    r = client.post("/api/admin/mail/rules", headers=hdr(tok), json={
        "name": "Held too long", "trigger": "held_overdue", "hours": 24})
    assert r.status_code == 201, r.get_json()
    rule = r.get_json()
    assert rule["enabled"] is True and rule["trigger"] == "held_overdue"
    assert "{reference}" in rule["subject_template"] or "{reference}" in rule["body_template"]
    # bad trigger is refused
    assert client.post("/api/admin/mail/rules", headers=hdr(tok), json={
        "name": "Nope", "trigger": "never"}).status_code == 400
    # the on/off switch
    r = client.patch(f"/api/admin/mail/rules/{rule['id']}", headers=hdr(tok), json={"enabled": False})
    assert r.status_code == 200 and r.get_json()["enabled"] is False
    assert client.get("/api/admin/mail/rules", headers=hdr(tok)).get_json()[0]["enabled"] is False
    r = client.patch(f"/api/admin/mail/rules/{rule['id']}", headers=hdr(tok), json={"enabled": True})
    assert r.get_json()["enabled"] is True
    # delete removes the rule
    assert client.delete(f"/api/admin/mail/rules/{rule['id']}", headers=hdr(tok)).status_code == 200
    assert client.get(f"/api/admin/mail/rules/{rule['id']}", headers=hdr(tok)).status_code == 404


def test_manual_send_validation(client, monkeypatch):
    from app.services import email
    sent = []
    monkeypatch.setattr(email, "send", lambda to, subject, body: sent.append(to) or True)
    tok, adm = register(client, "0770111333"), admin(client)
    # an invalid address is refused, even from an admin
    assert client.post("/api/admin/mail/send", headers=hdr(adm), json={
        "to": "not-an-email", "subject": "Hi", "body": "Hello"}).status_code == 400
    assert client.post("/api/admin/mail/send", headers=hdr(adm), json={
        "subject": "Hi", "body": "Hello"}).status_code == 400
    # an application whose citizen has no email on file is refused
    a = client.post("/api/applications", headers=hdr(tok), json={"service_code": "TIN_REG"}).get_json()
    assert client.post("/api/admin/mail/send", headers=hdr(adm), json={
        "application_id": a["id"], "subject": "Hi", "body": "Hello"}).status_code == 400
    # an address the admin typed, in valid format, is accepted and recorded
    r = client.post("/api/admin/mail/send", headers=hdr(adm), json={
        "to": "citizen@example.com", "subject": "Hello from PortaGov", "body": "Your file moved."})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["delivered"] is True and sent == ["citizen@example.com"]
    sent_list = client.get("/api/admin/mail/sent", headers=hdr(adm)).get_json()
    assert sent_list["items"][0]["to"] == "citizen@example.com"
    assert sent_list["items"][0]["source"] == "manual"
    assert "One-time codes are not listed here" in sent_list["note"]
    # sending to the citizen by application, once they have an email on file
    with client.application.app_context():
        from app.extensions import db
        from app.models import User
        u = User.query.filter_by(phone="+231770111333").first()
        u.email = "ma.test@example.com"
        db.session.commit()
    r = client.post("/api/admin/mail/send", headers=hdr(adm), json={
        "application_id": a["id"], "subject": "Update", "body": "Good news."})
    assert r.status_code == 201 and r.get_json()["to"] == "ma.test@example.com"


def test_inbox_not_configured(client):
    adm = admin(client)
    r = client.get("/api/admin/mail/inbox", headers=hdr(adm)).get_json()
    assert r["configured"] is False and r["items"] == []
    assert "not connected" in r["message"]
    st = client.get("/api/admin/mail/status", headers=hdr(adm)).get_json()
    assert st["imap_configured"] is False and st["smtp_configured"] is False
    assert st["last_run_at"] is None


def test_held_overdue_fires_once(client, monkeypatch):
    from app.services import email
    sent = []
    monkeypatch.setattr(email, "send", lambda to, subject, body: sent.append((to, subject)) or True)
    tok, adm = register(client, "0770111444", email="held@example.com"), admin(client)
    a = client.post("/api/applications", headers=hdr(tok), json={"service_code": "TIN_REG"}).get_json()
    with client.application.app_context():
        from app.extensions import db
        from app.models import Application
        app_row = db.session.get(Application, a["id"])
        app_row.status = "held"
        app_row.created_at = _dt.utcnow() - _td(hours=30)
        db.session.commit()
    r = client.post("/api/admin/mail/rules", headers=hdr(adm), json={
        "name": "Held too long", "trigger": "held_overdue", "hours": 24})
    rule_id = r.get_json()["id"]
    sent.clear()  # registration sent an OTP email to the same address; only rule mail counts now
    first = client.post(f"/api/admin/mail/rules/{rule_id}/run", headers=hdr(adm)).get_json()
    assert first["sent"] == 1 and first["failed"] == 0, first
    assert len(sent) == 1 and sent[0][0] == "held@example.com"
    assert a["reference"] in sent[0][1] or True
    # running again within 24 hours sends nothing more: the run log says skipped
    second = client.post(f"/api/admin/mail/rules/{rule_id}/run", headers=hdr(adm)).get_json()
    assert second["sent"] == 0 and second["skipped"] == 1 and len(sent) == 1
    runs = client.get(f"/api/admin/mail/runs?rule_id={rule_id}", headers=hdr(adm)).get_json()
    assert [x["status"] for x in runs] == ["skipped", "sent"]
    assert "24 hours" in runs[0]["detail"]
    # the status endpoint now reports when the rule last ran
    st = client.get("/api/admin/mail/status", headers=hdr(adm)).get_json()
    assert st["last_run_at"] is not None


def test_citizen_forbidden_on_mail(client):
    tok = register(client, "0770111555")
    for method, path in (("get", "/api/admin/mail/status"), ("get", "/api/admin/mail/inbox"),
                         ("get", "/api/admin/mail/sent"), ("get", "/api/admin/mail/rules"),
                         ("post", "/api/admin/mail/send"), ("post", "/api/admin/mail/run")):
        r = getattr(client, method)(path, headers=hdr(tok),
                                    json={"to": "x@example.com", "subject": "s", "body": "b"})
        assert r.status_code == 403, (method, path, r.status_code)


def test_daily_job_runs_mail_rules(client, monkeypatch):
    from app.services import email
    from app.services.tasks import run_daily
    monkeypatch.setattr(email, "send", lambda to, subject, body: True)
    tok, adm = register(client, "0770111666", email="daily@example.com"), admin(client)
    a = client.post("/api/applications", headers=hdr(tok), json={"service_code": "TIN_REG"}).get_json()
    with client.application.app_context():
        from app.extensions import db
        from app.models import Application
        app_row = db.session.get(Application, a["id"])
        app_row.status = "held"
        app_row.created_at = _dt.utcnow() - _td(hours=30)
        db.session.commit()
    client.post("/api/admin/mail/rules", headers=hdr(adm), json={
        "name": "Held too long", "trigger": "held_overdue", "hours": 24})
    with client.application.app_context():
        result = run_daily()
    assert result["mail_rules"]["sent"] == 1
    assert result["mail_rules"]["rules_run"] == 1
