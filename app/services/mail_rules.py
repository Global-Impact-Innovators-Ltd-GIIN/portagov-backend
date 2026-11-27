"""Automation rules that email citizens: held too long, a document missing, an
appointment tomorrow, a payment still pending, a sign-up never finished.

Rules run from the daily job (services/tasks.py run_daily) and from the admin
run-now endpoints; both call the same engine. Nothing here claims instant
delivery: every attempt is written to MailRuleRun with sent/skipped/failed and
the reason in plain words, and each rule records when it last ran.

SMS is sent only when a real SMS provider is live (see services/sms.py
status). SMS is still log-only, so normally no SMS is sent and the run log
says so.
"""
import re
from datetime import timedelta

from ..extensions import db
from ..models import (Appointment, Application, MailRule, MailRuleRun, Notification, Payment, User,
                      now)
from ..rules import normalize_doc_rules
from . import email as email_svc
from . import sms as sms_svc

TRIGGERS = ("held_overdue", "document_missing", "appointment_tomorrow", "payment_pending",
            "signup_incomplete")

DEFAULT_HOURS = {"held_overdue": 24, "payment_pending": 24, "signup_incomplete": 48,
                 "document_missing": 24, "appointment_tomorrow": 0}

DEFAULT_TEMPLATES = {
    "held_overdue": (
        "Your PortaGov application {reference} is waiting",
        "Hello {name},\n\nYour application {reference} for {service} has been waiting for "
        "{hours} hours. We are still checking it and will update you as soon as it moves.\n\n"
        "PortaGov, Citizens. Services. Connected."),
    "document_missing": (
        "A document is missing from your application {reference}",
        "Hello {name},\n\nYour application {reference} for {service} is missing a document we "
        "need. Please sign in to PortaGov and upload it so your application can move forward.\n\n"
        "PortaGov, Citizens. Services. Connected."),
    "appointment_tomorrow": (
        "Reminder: your PortaGov appointment is tomorrow",
        "Hello {name},\n\nThis is a reminder about your appointment tomorrow for application "
        "{reference} ({service}). Please bring the documents listed on your application.\n\n"
        "PortaGov, Citizens. Services. Connected."),
    "payment_pending": (
        "Your PortaGov payment for {reference} is still pending",
        "Hello {name},\n\nYour payment for application {reference} ({service}) has been pending "
        "for {hours} hours. If you already paid, please check your payment and try again, or "
        "contact PortaGov support.\n\nPortaGov, Citizens. Services. Connected."),
    "signup_incomplete": (
        "Finish setting up your PortaGov account",
        "Hello {name},\n\nYou created a PortaGov account {hours} hours ago but have not finished "
        "setting it up. Please sign in and complete your profile so you can apply for services.\n\n"
        "PortaGov, Citizens. Services. Connected."),
}

DEDUP_HOURS = 24
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render(template, ctx):
    """Fill {name}, {reference}, {service}, {hours} (and any extra keys). Unknown
    placeholders are left as they are, never an error."""
    return _PLACEHOLDER.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), template or "")


def _hours(rule):
    return rule.hours if rule.hours is not None else DEFAULT_HOURS.get(rule.trigger, 24)


def _already_sent(rule_id, application_id, user_id):
    """True when this rule already emailed this application or user in the last 24 hours."""
    since = now() - timedelta(hours=DEDUP_HOURS)
    q = MailRuleRun.query.filter(MailRuleRun.rule_id == rule_id, MailRuleRun.status == "sent",
                                 MailRuleRun.created_at >= since)
    if application_id:
        q = q.filter(MailRuleRun.application_id == application_id)
    elif user_id:
        q = q.filter(MailRuleRun.user_id == user_id)
    else:
        return False
    return q.first() is not None


def _log(rule, application_id, user_id, recipient, subject, status, detail):
    row = MailRuleRun(rule_id=rule.id, application_id=application_id, user_id=user_id,
                      recipient=recipient, subject=subject[:200], status=status, detail=detail[:300])
    db.session.add(row)
    return row


def _deliver(rule, user, application, subject, body):
    """Send one rule mail to the citizen on file. Returns (status, detail)."""
    app_id = application.id if application else None
    if not user or not (user.email or "").strip():
        return "skipped", "No email address on file for this citizen"
    if _already_sent(rule.id, app_id, user.id):
        return "skipped", "Already emailed in the last 24 hours"
    try:
        _msg, delivered = email_svc.send_manual(user.email.strip(), subject, body,
                                                application_id=app_id, user_id=user.id,
                                                source="automation")
    except ValueError as exc:
        return "failed", str(exc)
    if not delivered:
        return "failed", "SMTP is not configured, so the mail was recorded but not delivered"
    # In-portal notification (channel email). SMS only when a real provider is live.
    db.session.add(Notification(user_id=user.id, channel="email", title=subject[:150],
                                body=body[:400]))
    sms_live = sms_svc.status()["live"]
    if sms_live and user.phone:
        sms_svc.queue(user.phone, f"PortaGov: {subject}")
        sms_note = "SMS reminder also sent"
    else:
        sms_note = "No SMS sent: no live SMS provider (SMS is log-only)"
    return "sent", "Email sent. " + sms_note


def _candidates(rule):
    """(user, application) pairs this rule should email, before dedupe."""
    hours = _hours(rule)
    cutoff = now() - timedelta(hours=hours)
    if rule.trigger == "held_overdue":
        rows = Application.query.filter(Application.status == "held",
                                        Application.created_at < cutoff).all()
        return [(a.user, a) for a in rows]
    if rule.trigger == "document_missing":
        out = []
        rows = Application.query.filter(
            Application.status.in_(["held", "submitted", "in_review", "info_requested"])).all()
        for a in rows:
            required = [r["name"] for r in normalize_doc_rules(a.service.required_documents)
                        if r.get("required", True)]
            have = {d.doc_type for d in a.documents}
            if any(name not in have for name in required):
                out.append((a.user, a))
        return out
    if rule.trigger == "appointment_tomorrow":
        start = (now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        rows = Appointment.query.filter(Appointment.status == "booked",
                                        Appointment.slot >= start, Appointment.slot < end).all()
        return [(db.session.get(User, ap.user_id), db.session.get(Application, ap.application_id))
                for ap in rows]
    if rule.trigger == "payment_pending":
        rows = Payment.query.filter(Payment.status == "pending", Payment.created_at < cutoff).all()
        return [(p.application.user, p.application) for p in rows]
    if rule.trigger == "signup_incomplete":
        rows = User.query.filter(User.role == "citizen", User.created_at < cutoff).all()
        return [(u, None) for u in rows
                if not u.phone_verified or not (u.dob or "").strip() or not (u.county or "").strip()]
    return []


def run_rule(rule):
    """Run one rule now. Returns counts. The caller commits (this also saves
    last_run_at and the MailRuleRun rows)."""
    subject_t, body_t = DEFAULT_TEMPLATES.get(rule.trigger, ("PortaGov update", "Hello {name}."))
    subject_t = rule.subject_template or subject_t
    body_t = rule.body_template or body_t
    counts = {"sent": 0, "skipped": 0, "failed": 0}
    for user, application in _candidates(rule):
        ctx = {"name": (user.full_name if user else "there"),
               "reference": application.reference if application else "",
               "service": application.service.name if application else "",
               "hours": _hours(rule)}
        subject = render(subject_t, ctx)
        body = render(body_t, ctx)
        status, detail = _deliver(rule, user, application, subject, body)
        _log(rule, application.id if application else None, user.id if user else None,
             (user.email or "").strip() if user else "", subject, status, detail)
        counts[status] += 1
    rule.last_run_at = now()
    return {"rule_id": rule.id, "trigger": rule.trigger, **counts}


def run_all_rules():
    """Run every enabled rule. Returns totals plus a per-rule breakdown. The caller commits."""
    totals = {"sent": 0, "skipped": 0, "failed": 0, "rules_run": 0, "rules": []}
    for rule in MailRule.query.filter_by(enabled=True).order_by(MailRule.id).all():
        result = run_rule(rule)
        totals["rules_run"] += 1
        totals["sent"] += result["sent"]
        totals["skipped"] += result["skipped"]
        totals["failed"] += result["failed"]
        totals["rules"].append(result)
    return totals


__all__ = ["TRIGGERS", "DEFAULT_TEMPLATES", "render", "run_rule", "run_all_rules"]
