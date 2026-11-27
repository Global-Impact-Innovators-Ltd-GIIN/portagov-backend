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
import smtplib
from email.message import EmailMessage

from flask import current_app, g, has_request_context


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


def status():
    cfg = current_app.config
    missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS") if not cfg.get(k)]
    return {"configured": not missing, "missing": missing,
            "from": cfg.get("MAIL_FROM") or cfg.get("SMTP_USER")}
