"""Institution Approve flow: start review is idempotent, and marking an
application ready issues a pickup code the citizen can see.
See frontend issue #15: the desk showed 0 waiting, status changes did
not stick, and no pickup code ever reached the citizen.
"""

import io

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/t.db", "TESTING": True,
                      "UPLOAD_DIR": str(tmp_path / "up"), "EXPOSE_OTP": True})
    with app.app_context():
        from app.extensions import db
        from app.utils import set_setting
        set_setting("release_mode", "automatic")
        db.session.commit()
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


def register(c, phone):
    r = c.post("/api/auth/register", json={"full_name": "Ma Test", "phone": phone, "password": "Passw0rd!",
                                            "accept_terms": True})
    assert r.status_code == 201
    r = c.post("/api/auth/verify-otp", json={"phone": phone, "otp": r.get_json()["dev_otp"]})
    return r.get_json()["token"]


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff" + b"0" * 64
SAMPLE = {"pdf": b"%PDF-1.4 test file", "png": PNG, "jpg": JPG}


def upload_all(c, tok, aid):
    rules = c.get(f"/api/applications/{aid}", headers=hdr(tok)).get_json()["service_detail"]["required_documents"]
    for r in rules:
        ext = r["formats"][0]
        resp = c.post(f"/api/applications/{aid}/documents", headers=hdr(tok), content_type="multipart/form-data",
                      data={"file": (io.BytesIO(SAMPLE[ext]), "f." + ext), "doc_type": r["name"]})
        assert resp.status_code == 201, resp.get_json()


def _submitted(c, tok, adm, service="TIN_REG"):
    """A paid and released application, visible in the staff queue as submitted."""
    a = c.post("/api/applications", headers=hdr(tok), json={"service_code": service}).get_json()
    c.put(f"/api/applications/{a["id"]}", headers=hdr(tok),
          json={"data": {"full_name": "Ma Test", "nin": "LR12345678"}})
    upload_all(c, tok, a["id"])
    r = c.post(f"/api/applications/{a["id"]}/submit", headers=hdr(tok))
    assert r.status_code == 200, r.get_json()
    rows = c.get("/api/staff/applications", headers=hdr(adm)).get_json()
    mine = [x for x in rows if x["id"] == a["id"]]
    assert mine and mine[0]["status"] == "submitted"
    return a


def _action(c, adm, aid, action):
    return c.post(f"/api/staff/applications/{aid}/action", headers=hdr(adm), json={"action": action})


def test_start_review_is_idempotent(client):
    tok = register(client, "0770111222")
    adm = login(client, "+231770000001", "Admin@12345")
    a = _submitted(client, tok, adm)
    assert _action(client, adm, a["id"], "start_review").status_code == 200
    again = _action(client, adm, a["id"], "start_review")
    assert again.status_code == 200
    assert again.get_json()["status"] == "in_review"
    assert again.get_json()["stage"]["index"] == 0


def test_ready_issues_pickup_code(client):
    tok = register(client, "0770111333")
    adm = login(client, "+231770000001", "Admin@12345")
    a = _submitted(client, tok, adm)
    for act in ("start_review", "approve"):
        assert _action(client, adm, a["id"], act).status_code == 200
    r = _action(client, adm, a["id"], "ready")
    assert r.status_code == 200
    code = r.get_json()["pickup_code"]
    assert code and len(code) == 6 and code.isdigit()
    mine = client.get(f"/api/applications/{a["id"]}", headers=hdr(tok)).get_json()
    assert mine["status"] == "ready_for_collection"
    assert mine["pickup_code"] == code
    notes = client.get("/api/notifications", headers=hdr(tok)).get_json()
    assert any(code in (n.get("body") or "") for n in notes)
