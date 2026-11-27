"""Help desk tickets: assistant handover and the AI-down capture path."""
import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/tk.db", "TESTING": True,
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
    r = c.post("/api/auth/register", json={"full_name": "Ma Test", "phone": phone,
                                           "password": "Passw0rd!", "accept_terms": True})
    assert r.status_code == 201, r.get_json()
    r = c.post("/api/auth/verify-otp", json={"phone": phone, "otp": r.get_json()["dev_otp"]})
    return r.get_json()["token"]


def admin(c):
    return login(c, "+231770000001", "Admin@12345")


def make_application(c, tok, phone):
    """A submitted application row for the citizen, on a Ministry of Health service."""
    from app.extensions import db
    from app.models import Application, Institution, Service, User
    from app.utils import gen_reference
    with c.application.app_context():
        user = User.query.filter_by(phone="+231770111222" if phone == "0770111222" else phone).first()
        svc = Service.query.join(Institution).filter(Institution.code == "MOH").first()
        assert svc is not None
        app = Application(reference=gen_reference(), user_id=user.id, service_id=svc.id,
                          status="submitted", fee_usd=3)
        db.session.add(app)
        db.session.commit()
        return app.id, app.reference, svc.institution_id


def test_ticket_create_citizen_mine_and_by_phone(client):
    tok = register(client)
    r = client.post("/api/tickets", headers=hdr(tok), json={
        "name": "Ma Test", "phone": "0770111222", "topic": "Help desk",
        "message": "How do I track my application?", "source": "assistant"})
    assert r.status_code == 201, r.get_json()
    t = r.get_json()
    assert t["status"] == "New" and t["source"] == "assistant" and t["reference"] is None
    mine = client.get("/api/tickets/mine", headers=hdr(tok)).get_json()["requests"]
    assert len(mine) == 1 and mine[0]["id"] == t["id"]
    by_phone = client.get("/api/tickets/by-phone", headers=hdr(tok),
                          query_string={"phone": "0770111222"}).get_json()["requests"]
    assert len(by_phone) == 1
    # another citizen's phone is refused
    assert client.get("/api/tickets/by-phone", headers=hdr(tok),
                      query_string={"phone": "0770999888"}).status_code == 403
    # validation: short message, bad phone, missing name
    assert client.post("/api/tickets", headers=hdr(tok), json={
        "name": "Ma Test", "phone": "0770111222", "topic": "Help desk", "message": "hi"}).status_code == 400
    assert client.post("/api/tickets", json={
        "name": "Guest", "phone": "123", "topic": "Help desk",
        "message": "How do I track my application?"}).status_code == 400
    assert client.post("/api/tickets", json={
        "phone": "0770111333", "topic": "Help desk",
        "message": "How do I track my application?"}).status_code == 400


def test_ticket_create_guest_and_application_link(client):
    tok = register(client)
    app_id, ref, _inst = make_application(client, tok, "0770111222")
    # guest with the matching phone links the application and its tracking number
    r = client.post("/api/tickets", json={
        "name": "Ma Test", "phone": "0770111222", "topic": "Help desk",
        "message": "Where is my birth certificate?", "application_id": app_id, "source": "assistant"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["reference"] == ref and r.get_json()["application_id"] == app_id
    # guest with a different phone cannot link it
    assert client.post("/api/tickets", json={
        "name": "Someone Else", "phone": "0770999888", "topic": "Help desk",
        "message": "Where is my birth certificate?", "application_id": app_id}).status_code == 400
    # another signed-in citizen cannot link it either
    tok2 = register(client, "0770222333")
    assert client.post("/api/tickets", headers=hdr(tok2), json={
        "name": "Other", "phone": "0770222333", "topic": "Help desk",
        "message": "Where is my birth certificate?", "application_id": app_id}).status_code == 403
    # unknown application is refused
    assert client.post("/api/tickets", headers=hdr(tok), json={
        "name": "Ma Test", "phone": "0770111222", "topic": "Help desk",
        "message": "Where is my birth certificate?", "application_id": 99999}).status_code == 404


def test_assistant_matched_flag(client):
    r = client.post("/api/assistant", json={"q": "how much is a birth certificate?"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["matched"] is True and r.get_json()["answer"]
    r = client.post("/api/assistant", json={"q": "what is the weather on Mars"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["matched"] is False


def test_staff_queue_reply_assign_and_scope(client):
    tok = register(client)
    app_id, ref, moh_id = make_application(client, tok, "0770111222")
    r = client.post("/api/tickets", headers=hdr(tok), json={
        "name": "Ma Test", "phone": "0770111222", "topic": "Help desk",
        "message": "Where is my birth certificate?", "application_id": app_id, "source": "assistant"})
    assert r.status_code == 201, r.get_json()
    tid = r.get_json()["id"]
    # a general ticket with no application
    r = client.post("/api/tickets", headers=hdr(tok), json={
        "name": "Ma Test", "phone": "0770111222", "topic": "Payments desk",
        "message": "My payment did not confirm."})
    assert r.status_code == 201, r.get_json()
    general_id = r.get_json()["id"]

    adm = admin(client)
    insts = {i["code"]: i["id"] for i in client.get("/api/institutions").get_json()}
    other = next(code for code in insts if code != "MOH")
    mk = lambda body: client.post("/api/admin/officers", headers=hdr(adm),
                                  json={"password": "Staff0pass!", **body})
    assert mk({"full_name": "MOH Officer", "phone": "0770444010", "role": "officer",
               "institution_id": moh_id}).status_code == 201
    assert mk({"full_name": "Other Officer", "phone": "0770444011", "role": "officer",
               "institution_id": insts[other]}).status_code == 201
    moh = login(client, "0770444010", "Staff0pass!")
    oth = login(client, "0770444011", "Staff0pass!")

    # admin sees everything; the MOH officer sees the linked ticket and the
    # general one; the other officer sees only the general one
    assert len(client.get("/api/staff/tickets", headers=hdr(adm)).get_json()["requests"]) == 2
    moh_list = client.get("/api/staff/tickets", headers=hdr(moh)).get_json()["requests"]
    assert {t["id"] for t in moh_list} == {tid, general_id}
    oth_list = client.get("/api/staff/tickets", headers=hdr(oth)).get_json()["requests"]
    assert {t["id"] for t in oth_list} == {general_id}
    # the other officer cannot touch the MOH ticket at all
    assert client.post(f"/api/staff/tickets/{tid}/reply", headers=hdr(oth),
                       json={"reply": "Hello"}).status_code == 404
    # assign moves a new ticket to In progress
    r = client.post(f"/api/staff/tickets/{tid}/assign", headers=hdr(adm),
                    json={"officer_name": "MOH Officer"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/api/staff/tickets?status=In+progress", headers=hdr(adm)) \
        .get_json()["requests"][0]["assigned_to"] == "MOH Officer"
    # reply answers it and the citizen sees the reply on their own ticket
    r = client.post(f"/api/staff/tickets/{tid}/reply", headers=hdr(moh),
                    json={"reply": "Your certificate is ready for collection."})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    mine = client.get("/api/tickets/mine", headers=hdr(tok)).get_json()["requests"]
    done = next(t for t in mine if t["id"] == tid)
    assert done["status"] == "Answered"
    assert done["reply"] == "Your certificate is ready for collection."
    assert done["replies"] and done["replies"][0]["sender"] == "staff"
    # bad status and short reply are refused
    assert client.post(f"/api/staff/tickets/{tid}/status", headers=hdr(adm),
                       json={"status": "Done"}).status_code == 400
    assert client.post(f"/api/staff/tickets/{tid}/reply", headers=hdr(adm),
                       json={"reply": "x"}).status_code == 400
    # citizens cannot use the staff queue
    assert client.get("/api/staff/tickets", headers=hdr(tok)).status_code == 403
    assert client.post(f"/api/staff/tickets/{general_id}/reply", headers=hdr(tok),
                       json={"reply": "Hello"}).status_code == 403
