"""Email delivery for one-time codes.

SMTP settings come from the environment only (see Config):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM

No settings means no email is sent: codes still go out by SMS exactly as
before, and a missing mail setup never breaks a request. Port 465 uses
implicit SSL; any other port uses STARTTLS.

Messages are queued during a request and only sent once the response
succeeded, so a request that fails and rolls back never emails a code
that no longer exists on the server.
"""
import re
import smtplib
from email.message import EmailMessage

from flask import current_app, g, has_request_context

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(addr):
    return bool(addr and EMAIL_RE.match(str(addr).strip()))


def _configured():
    cfg = current_app.config
    return bool(cfg.get("SMTP_HOST") and cfg.get("SMTP_USER") and cfg.get("SMTP_PASS"))


def queue(to, subject, body):
    if not to or not subject or not body:
        return
    if has_request_context():
        g.setdefault("_email_outbox", []).append((to, subject, body))
    else:
        send(to, subject, body)


def flush(response):
    """after_request hook: send queued mail when the request succeeded."""
    outbox = g.pop("_email_outbox", []) if has_request_context() else []
    if response.status_code < 400:
        for to, subject, body in outbox:
            send(to, subject, body)
    return response


def send(to, subject, body):
    cfg = current_app.config
    if not _configured():
        current_app.logger.info("Email (not sent, SMTP not configured) to %s***", (to or "")[:4])
        return False
    msg = EmailMessage()
    msg["From"] = cfg.get("MAIL_FROM") or cfg["SMTP_USER"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        port = int(cfg.get("SMTP_PORT") or 587)
        if port == 465:
            server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, timeout=10)
        else:
            server = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=10)
            server.starttls()
        with server:
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            server.send_message(msg)
        current_app.logger.info("Email sent via SMTP to %s***", (to or "")[:4])
        return True
    except Exception as exc:  # never break the user's request because email failed
        current_app.logger.error("Email via SMTP failed: %s", exc)
        return False


OTP_SUBJECTS = {
    "verify": "Your PortaGov verification code",
    "login": "Your PortaGov sign-in code",
    "reset": "Your PortaGov password reset code",
}

OTP_LINES = {
    "verify": "Your PortaGov verification code is {c}.",
    "login": "{c} is your PortaGov sign-in code.",
    "reset": "{c} is your PortaGov password reset code.",
}


def send_otp_email(to, code, purpose="verify"):
    """Queue a one-time code email. Nothing is sent until the request succeeds."""
    if not to:
        return
    line = OTP_LINES.get(purpose, OTP_LINES["verify"]).format(c=code)
    body = (
        "Hello,\n\n"
        f"{line} It expires in 10 minutes. Never share it with anyone.\n\n"
        "If you did not ask for this code, please ignore this email.\n\n"
        "PortaGov, Citizens. Services. Connected."
    )
    queue(to, OTP_SUBJECTS.get(purpose, OTP_SUBJECTS["verify"]), body)


def send_manual(to, subject, body, application_id=None, user_id=None, sent_by=None, source="manual"):
    """Send one mail now (an admin action or an automation rule) and record it as a
    MailMessage (direction out) plus an audit entry. Returns (MailMessage, delivered).
    The row is written even when SMTP is not configured, so the Sent list is an honest
    record: `delivered` is False then, and the caller must say so. Raises ValueError
    for a missing or malformed recipient. The caller commits."""
    from ..models import MailMessage
    to = (to or "").strip()
    if not is_valid_email(to):
        raise ValueError("Enter a valid email address")
    if not subject or not (body or "").strip():
        raise ValueError("Subject and body are required")
    msg = MailMessage(direction="out", from_addr=current_app.config.get("MAIL_FROM"),
                      to_addr=to, subject=subject[:200], body=body, application_id=application_id,
                      user_id=user_id, sent_by=sent_by, source=source)
    from ..extensions import db
    db.session.add(msg)
    db.session.flush()  # give the row an id before the audit entry names it
    if has_request_context():
        from ..utils import audit
        audit("mail_send", f"{source} mail to {to} (id {msg.id})", user_id=sent_by)
    delivered = send(to, subject, body)
    return msg, delivered


def status():
    cfg = current_app.config
    missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS") if not cfg.get(k)]
    return {"configured": not missing, "missing": missing,
            "from": cfg.get("MAIL_FROM") or cfg.get("SMTP_USER")}
