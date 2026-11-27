"""Announcements API: public list, admin post and delete."""
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


def register(c, phone="0770111222"):
    body = {"full_name": "Ma Test", "phone": phone, "password": "Passw0rd!", "accept_terms": True}
    r = c.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.get_json()
    r = c.post("/api/auth/verify-otp", json={"phone": phone, "otp": r.get_json()["dev_otp"]})
    return r.get_json()["token"]


def admin(c):
    return login(c, "+231770000001", "Admin@12345")


def post(c, tok, **kw):
    body = {"ministry": "PortaGov", "title": "Passport office opens Saturday",
            "body": "The passport office opens this Saturday from 9am to 1pm."}
    body.update(kw)
    return c.post("/api/admin/announcements", headers=hdr(tok), json=body)


def test_public_list_starts_empty(client):
    r = client.get("/api/announcements")
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"announcements": []}


def test_post_requires_admin(client):
    # no token at all
    assert client.post("/api/admin/announcements", json={
        "ministry": "PortaGov", "title": "Hello", "body": "Hello world"}).status_code == 401
    # a citizen token is not staff
    tok = register(client)
    assert post(client, tok).status_code == 403
    # an admin token works
    r = post(client, admin(client))
    assert r.status_code == 201, r.get_json()
    row = r.get_json()
    assert row["ministry"] == "PortaGov" and row["urgent"] is False
    assert row["created_at"] and row["id"]


def test_post_validation(client):
    tok = admin(client)
    assert post(client, tok, title="Hi").status_code == 400
    assert post(client, tok, body="tiny").status_code == 400
    assert post(client, tok, ministry="").status_code == 400
    assert post(client, tok, title="x" * 161).status_code == 400


def test_list_newest_first_ministry_and_limit(client):
    tok = admin(client)
    post(client, tok, ministry="Ministry of Health", title="Clinic hours change",
         body="The clinic opens an hour later on Mondays.")
    post(client, tok, ministry="PortaGov", title="Urgent notice", body="Please read this now.",
         urgent=True)
    post(client, tok, ministry="PortaGov", title="Second notice", body="Another platform notice.")
    rows = client.get("/api/announcements").get_json()["announcements"]
    assert [r["title"] for r in rows] == ["Second notice", "Urgent notice", "Clinic hours change"]
    assert rows[1]["urgent"] is True and rows[0]["urgent"] is False
    health = client.get("/api/announcements?ministry=Ministry of Health").get_json()["announcements"]
    assert [r["title"] for r in health] == ["Clinic hours change"]
    one = client.get("/api/announcements?limit=1").get_json()["announcements"]
    assert len(one) == 1 and one[0]["title"] == "Second notice"
    assert client.get("/api/announcements?limit=abc").status_code == 400
    assert client.get("/api/announcements?limit=500").get_json()["announcements"].__len__() == 3


def test_delete_flow(client):
    tok = admin(client)
    row = post(client, tok).get_json()
    ann_id = row["id"]
    # no token, and a citizen token, cannot delete
    assert client.delete(f"/api/admin/announcements/{ann_id}").status_code == 401
    assert client.delete(f"/api/admin/announcements/{ann_id}",
                         headers=hdr(register(client, "0770111333"))).status_code == 403
    # admin deletes, the notice is gone, a second delete is a 404
    r = client.delete(f"/api/admin/announcements/{ann_id}", headers=hdr(tok))
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert client.get("/api/announcements").get_json() == {"announcements": []}
    assert client.delete(f"/api/admin/announcements/{ann_id}", headers=hdr(tok)).status_code == 404
    assert client.delete("/api/admin/announcements/99999", headers=hdr(tok)).status_code == 404
