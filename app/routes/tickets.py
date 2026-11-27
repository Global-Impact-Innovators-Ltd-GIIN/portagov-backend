"""Help desk tickets: the assistant handover and the AI-down capture path.

Every question the assistant cannot answer becomes a ticket here, and every
question asked when the answer path itself is down is captured here the
moment it is asked, so it is never lost. A ticket is linked to the citizen's
application and tracking number when there is one, so every reply stays on
that same application.

Citizens see only their own tickets. Staff see tickets by role and queue:
platform admins see all, institution officers see tickets on their own
institution's applications plus general tickets with no application.
"""
import jwt
from flask import Blueprint, current_app, g, jsonify, request

from ..extensions import db
from ..models import Application, HelpTicket, HelpTicketReply, Service, User, now
from ..utils import audit, auth_required, err, normalize_phone, notify, rate_limit

bp = Blueprint("tickets", __name__)

REVIEWERS = ("officer", "inst_admin", "admin")
INSTITUTION_ROLES = ("officer", "inst_admin")
STATUSES = ("New", "In progress", "Answered")
SOURCES = ("assistant", "help_page", "callback")


def _optional_user():
    """The signed-in user when a valid Bearer token is present, else None.
    A bad or expired token is not an error here: the ticket is still
    captured, as a guest ticket, so the question is never lost."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(header[7:], current_app.config["SECRET_KEY"], algorithms=["HS256"])
        user = db.session.get(User, int(payload["sub"]))
        if not user or user.disabled or payload.get("v", 0) != (user.token_version or 0):
            return None
        return user
    except Exception:
        return None


def _staff_ticket_or_404(ticket_id):
    """A ticket this staff member may work on, else None. Out-of-scope
    tickets answer 404, so one institution never learns another's tickets exist."""
    t = db.session.get(HelpTicket, ticket_id)
    if not t:
        return None
    if g.user.role in INSTITUTION_ROLES and t.application_id:
        app = db.session.get(Application, t.application_id)
        if not app or app.service.institution_id != g.user.institution_id:
            return None
    return t


@bp.post("/api/tickets")
@rate_limit("ticket-create", 20, 3600)
def create():
    """Create a ticket. Signed-in citizens are linked by their account;
    guests give a name and phone. The question is stored at once."""
    d = request.get_json(silent=True) or {}
    user = _optional_user()
    name = (d.get("name") or (user.full_name if user else "")).strip()
    phone = normalize_phone(d.get("phone") or (user.phone if user else ""))
    topic = (d.get("topic") or "Help desk").strip()[:80]
    message = (d.get("message") or "").strip()
    source = (d.get("source") or "help_page").strip()[:20]
    if source not in SOURCES:
        source = "help_page"
    if len(name) < 2:
        return err("Tell us your name so we can answer you", 400)
    if not phone:
        return err("Give a phone number we can reach you on", 400)
    if len(topic) < 2:
        return err("Choose what your question is about", 400)
    if len(message) < 5:
        return err("Write your question in a few words", 400)
    app = None
    if d.get("application_id"):
        try:
            app = db.session.get(Application, int(d["application_id"]))
        except (TypeError, ValueError):
            app = None
        if not app:
            return err("That application was not found", 404)
        if user and app.user_id != user.id:
            return err("That application is not yours", 403)
        if not user and normalize_phone(app.user.phone) != phone:
            return err("That application does not match this phone number", 400)
    t = HelpTicket(user_id=user.id if user else None, name=name, phone=phone, topic=topic,
                   message=message, application_id=app.id if app else None,
                   reference=app.reference if app else None, source=source)
    db.session.add(t)
    db.session.commit()
    audit("ticket_created", f"Ticket {t.id} ({topic})" + (f" on {t.reference}" if t.reference else ""),
          user_id=user.id if user else None)
    return jsonify(t.to_dict()), 201


@bp.get("/api/tickets/mine")
@auth_required()
def mine():
    """The signed-in citizen's own tickets, newest first, with replies."""
    rows = (HelpTicket.query.filter_by(user_id=g.user.id)
            .order_by(HelpTicket.id.desc()).limit(100).all())
    return jsonify({"requests": [t.to_dict() for t in rows]})


@bp.get("/api/tickets/by-phone")
@auth_required()
def by_phone():
    """Tickets for one phone number. A citizen may only ask for their own
    phone; staff may ask for any phone while helping that citizen."""
    phone = normalize_phone(request.args.get("phone") or "")
    if not phone:
        return err("Give a phone number", 400)
    if g.user.role == "citizen" and normalize_phone(g.user.phone) != phone:
        return err("You can only see your own tickets", 403)
    rows = (HelpTicket.query.filter_by(phone=phone)
            .order_by(HelpTicket.id.desc()).limit(100).all())
    return jsonify({"requests": [t.to_dict() for t in rows]})


@bp.get("/api/staff/tickets")
@auth_required(*REVIEWERS)
def staff_list():
    """The help desk queue, by role: platform admins see all, institution
    officers see their own institution's tickets plus general tickets with
    no application. Filter by status and queue (topic)."""
    q = HelpTicket.query
    if g.user.role in INSTITUTION_ROLES:
        q = (q.outerjoin(Application, HelpTicket.application_id == Application.id)
              .outerjoin(Service, Application.service_id == Service.id)
              .filter((Service.institution_id == g.user.institution_id)
                      | (HelpTicket.application_id.is_(None))))
    status = request.args.get("status")
    if status:
        if status not in STATUSES:
            return err("Choose New, In progress or Answered", 400)
        q = q.filter(HelpTicket.status == status)
    queue = request.args.get("queue")
    if queue:
        q = q.filter(HelpTicket.topic == queue)
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 100)
    except ValueError:
        limit = 50
    rows = q.order_by(HelpTicket.id.desc()).limit(limit).all()
    return jsonify({"requests": [t.to_dict() for t in rows]})


@bp.post("/api/staff/tickets/<int:ticket_id>/reply")
@auth_required(*REVIEWERS)
def reply(ticket_id):
    """A staff reply. Sets the ticket Answered, stores the reply on the
    ticket, and tells the citizen in their portal notifications."""
    t = _staff_ticket_or_404(ticket_id)
    if not t:
        return err("Ticket not found", 404)
    body = ((request.get_json(silent=True) or {}).get("reply") or "").strip()
    if len(body) < 2:
        return err("Write the reply first", 400)
    db.session.add(HelpTicketReply(ticket_id=t.id, sender="staff",
                                  sender_name=g.user.full_name, body=body))
    t.reply = body
    t.status = "Answered"
    t.answered_at = now()
    db.session.commit()
    if t.user_id:
        notify(t.user_id, "Help desk replied",
               f"PortaGov answered your question about {t.topic}.")
    audit("ticket_replied", f"Ticket {t.id} answered by {g.user.full_name}", user_id=g.user.id)
    return jsonify({"ok": True})


@bp.post("/api/staff/tickets/<int:ticket_id>/status")
@auth_required(*REVIEWERS)
def set_status(ticket_id):
    """Move a ticket between New, In progress and Answered."""
    t = _staff_ticket_or_404(ticket_id)
    if not t:
        return err("Ticket not found", 404)
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in STATUSES:
        return err("Choose New, In progress or Answered", 400)
    t.status = status
    if status == "Answered" and not t.answered_at:
        t.answered_at = now()
    db.session.commit()
    audit("ticket_status", f"Ticket {t.id} set to {status} by {g.user.full_name}", user_id=g.user.id)
    return jsonify(t.to_dict())


@bp.post("/api/staff/tickets/<int:ticket_id>/assign")
@auth_required(*REVIEWERS)
def assign(ticket_id):
    """Hand a ticket to a named support person. A new ticket moves to
    In progress when it is assigned."""
    t = _staff_ticket_or_404(ticket_id)
    if not t:
        return err("Ticket not found", 404)
    name = ((request.get_json(silent=True) or {}).get("officer_name") or "").strip()
    if len(name) < 2:
        return err("Name the person taking this ticket", 400)
    t.assigned_to = name[:150]
    if t.status == "New":
        t.status = "In progress"
    db.session.commit()
    audit("ticket_assigned", f"Ticket {t.id} assigned to {t.assigned_to} by {g.user.full_name}",
          user_id=g.user.id)
    return jsonify({"ok": True})
