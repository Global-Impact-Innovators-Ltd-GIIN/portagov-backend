"""Platform email: inbox, manual send, and automation rules.

All endpoints are admin-only (plus the 'support' permission gate used in
staff.py). The Sent list covers manual and automation mail only: one-time
codes are NOT listed there, they are recorded as notifications, and the
response says so.
"""
from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Application, Institution, MailMessage, MailRule, MailRuleRun, User, now
from ..services import email as email_svc
from ..services import mailbox, mail_rules
from ..utils import audit, auth_required, err, need, rate_limit

bp = Blueprint("mail", __name__, url_prefix="/api/admin/mail")

SENT_NOTE = ("One-time codes are not listed here. They are recorded as notifications, "
             "not as sent mail.")


def _gate():
    return need("support")


def _rule_or_404(rule_id):
    rule = db.session.get(MailRule, rule_id)
    if not rule:
        return None, err("Rule not found", 404)
    return rule, None


@bp.get("/status")
@auth_required("admin")
def status():
    gate = _gate()
    if gate:
        return gate
    last = db.session.query(MailRule.last_run_at).filter(MailRule.last_run_at.isnot(None)) \
        .order_by(MailRule.last_run_at.desc()).first()
    return jsonify({"imap_configured": mailbox.status()["configured"],
                    "smtp_configured": email_svc.status()["configured"],
                    "last_run_at": last[0].isoformat() if last else None,
                    "rules_enabled": MailRule.query.filter_by(enabled=True).count(),
                    "rules_total": MailRule.query.count()})


@bp.get("/inbox")
@auth_required("admin")
def inbox():
    gate = _gate()
    if gate:
        return gate
    st = mailbox.status()
    if not st["configured"]:
        return jsonify({"configured": False, "items": [],
                        "message": "Email inbox is not connected. Set IMAP_HOST, IMAP_USER and "
                                   "IMAP_PASS in Render to connect the platform mailbox."})
    sync = mailbox.sync()
    items = MailMessage.query.filter_by(direction="in").order_by(MailMessage.id.desc()).limit(100).all()
    out = {"configured": True, "synced": sync["synced"], "items": [m.to_dict() for m in items]}
    if sync["error"]:
        out["sync_error"] = ("Could not reach the mailbox just now, showing mail already stored: "
                             + sync["error"])
    return jsonify(out)


@bp.post("/inbox/<int:msg_id>/read")
@auth_required("admin")
def mark_read(msg_id):
    gate = _gate()
    if gate:
        return gate
    m = db.session.get(MailMessage, msg_id)
    if not m or m.direction != "in":
        return err("Message not found", 404)
    m.read = True
    db.session.commit()
    return jsonify(m.to_dict())


@bp.get("/sent")
@auth_required("admin")
def sent():
    gate = _gate()
    if gate:
        return gate
    items = MailMessage.query.filter_by(direction="out").order_by(MailMessage.id.desc()).limit(100).all()
    return jsonify({"items": [m.to_dict() for m in items], "note": SENT_NOTE})


@bp.post("/send")
@auth_required("admin")
@rate_limit("mail_send", 20, 3600)
def send_mail():
    """Send one mail. Body: to OR application_id OR user_id, plus subject and body.
    `to` is an address the admin typed (valid format required); application_id /
    user_id resolve to the citizen email on file. Nothing else is ever mailed."""
    gate = _gate()
    if gate:
        return gate
    d = request.get_json(silent=True) or {}
    subject = (d.get("subject") or "").strip()
    body = (d.get("body") or "").strip()
    if not subject or not body:
        return err("Subject and body are required")
    to, app_id, user_id = (d.get("to") or "").strip(), d.get("application_id"), d.get("user_id")
    if to:
        if not email_svc.is_valid_email(to):
            return err("Enter a valid email address")
    elif app_id:
        a = db.session.get(Application, app_id)
        if not a:
            return err("Application not found", 404)
        to, user_id = (a.user.email or "").strip(), a.user_id
        if not to:
            return err("That citizen has no email address on file")
    elif user_id:
        u = db.session.get(User, user_id)
        if not u:
            return err("User not found", 404)
        to = (u.email or "").strip()
        if not to:
            return err("That citizen has no email address on file")
    else:
        return err("Give an address to send to, or an application_id or user_id")
    # A typed address is allowed as typed; an address from a file is only ever
    # the citizen's own email or an institution contact email. Refuse anything else.
    if not to:
        return err("Enter a valid email address")
    on_file = (User.query.filter(User.email.isnot(None)).filter(
        db.func.lower(User.email) == to.lower()).first() is not None
        or Institution.query.filter(Institution.contact_email.isnot(None)).filter(
            db.func.lower(Institution.contact_email) == to.lower()).first() is not None)
    typed = bool(d.get("to"))
    if not (typed or on_file):
        return err("That address is not a citizen or institution email on file")
    try:
        msg, delivered = email_svc.send_manual(to, subject, body, application_id=app_id,
                                               user_id=user_id, sent_by=g.user.id, source="manual")
    except ValueError as exc:
        return err(str(exc))
    db.session.commit()
    out = msg.to_dict()
    out["delivered"] = delivered
    if not delivered:
        out["note"] = "SMTP is not configured, so this mail was recorded but not delivered."
    return jsonify(out), 201


@bp.get("/rules")
@auth_required("admin")
def list_rules():
    gate = _gate()
    if gate:
        return gate
    return jsonify([r.to_dict() for r in MailRule.query.order_by(MailRule.id).all()])


@bp.post("/rules")
@auth_required("admin")
def create_rule():
    gate = _gate()
    if gate:
        return gate
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    trigger = (d.get("trigger") or "").strip()
    if not name:
        return err("Give the rule a name")
    if trigger not in mail_rules.TRIGGERS:
        return err("trigger must be one of: " + ", ".join(mail_rules.TRIGGERS))
    hours = d.get("hours")
    if hours is not None:
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            return err("hours must be a whole number")
        if hours < 0:
            return err("hours must be 0 or more")
    subject_t, body_t = mail_rules.DEFAULT_TEMPLATES[trigger]
    rule = MailRule(name=name[:120], trigger=trigger, hours=hours,
                    subject_template=(d.get("subject_template") or subject_t)[:200],
                    body_template=d.get("body_template") or body_t,
                    enabled=bool(d.get("enabled", True)), created_by=g.user.id)
    db.session.add(rule)
    audit("mail_rule_create", f"{trigger} '{name}'")
    db.session.commit()
    return jsonify(rule.to_dict()), 201


@bp.get("/rules/<int:rule_id>")
@auth_required("admin")
def get_rule(rule_id):
    gate = _gate()
    if gate:
        return gate
    rule, error = _rule_or_404(rule_id)
    return error or jsonify(rule.to_dict())


@bp.patch("/rules/<int:rule_id>")
@auth_required("admin")
def update_rule(rule_id):
    """Patch covers the on/off switch: {"enabled": false} turns a rule off."""
    gate = _gate()
    if gate:
        return gate
    rule, error = _rule_or_404(rule_id)
    if error:
        return error
    d = request.get_json(silent=True) or {}
    if "name" in d and (d.get("name") or "").strip():
        rule.name = d["name"].strip()[:120]
    if "hours" in d:
        if d["hours"] is None:
            rule.hours = None
        else:
            try:
                rule.hours = int(d["hours"])
            except (TypeError, ValueError):
                return err("hours must be a whole number")
    if "subject_template" in d and d.get("subject_template"):
        rule.subject_template = d["subject_template"][:200]
    if "body_template" in d and d.get("body_template"):
        rule.body_template = d["body_template"]
    if "enabled" in d:
        rule.enabled = bool(d["enabled"])
    audit("mail_rule_update", f"rule {rule.id} enabled={rule.enabled}")
    db.session.commit()
    return jsonify(rule.to_dict())


@bp.delete("/rules/<int:rule_id>")
@auth_required("admin")
def delete_rule(rule_id):
    gate = _gate()
    if gate:
        return gate
    rule, error = _rule_or_404(rule_id)
    if error:
        return error
    MailRuleRun.query.filter_by(rule_id=rule.id).delete()
    audit("mail_rule_delete", f"{rule.trigger} '{rule.name}'")
    db.session.delete(rule)
    db.session.commit()
    return jsonify({"ok": True})


@bp.post("/rules/<int:rule_id>/run")
@auth_required("admin")
def run_one_rule(rule_id):
    """Run one rule now, even if it is switched off: the admin asked for it now.
    The rule's last_run_at is updated, so the console always shows when it last ran."""
    gate = _gate()
    if gate:
        return gate
    rule, error = _rule_or_404(rule_id)
    if error:
        return error
    result = mail_rules.run_rule(rule)
    audit("mail_rule_run", f"rule {rule.id} sent={result['sent']} skipped={result['skipped']}")
    db.session.commit()
    result["last_run_at"] = rule.last_run_at.isoformat() if rule.last_run_at else None
    result["enabled"] = bool(rule.enabled)
    return jsonify(result)


@bp.post("/run")
@auth_required("admin")
def run_all():
    """Run all enabled rules now. Same engine the daily job uses."""
    gate = _gate()
    if gate:
        return gate
    result = mail_rules.run_all_rules()
    audit("mail_rules_run", f"rules={result['rules_run']} sent={result['sent']}")
    db.session.commit()
    return jsonify(result)


@bp.get("/runs")
@auth_required("admin")
def list_runs():
    gate = _gate()
    if gate:
        return gate
    q = MailRuleRun.query
    if request.args.get("rule_id"):
        try:
            q = q.filter(MailRuleRun.rule_id == int(request.args["rule_id"]))
        except ValueError:
            return err("rule_id must be a number")
    return jsonify([r.to_dict() for r in q.order_by(MailRuleRun.id.desc()).limit(200).all()])
