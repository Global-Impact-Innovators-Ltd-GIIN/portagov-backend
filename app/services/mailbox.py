"""Read the platform mailbox over IMAP (StackMail on ServerByt).

Nothing here ever logs or returns a password. When IMAP is not configured, or
the login fails, the caller gets a plain ``{configured: false}`` or an honest
error string: the request never crashes because mail is down.
"""
import hashlib
import imaplib
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime

from flask import current_app

from ..extensions import db
from ..models import MailMessage, now

SNIPPET_CHARS = 1000
FETCH_LIMIT = 50


def status():
    """Whether the inbox can be read, without revealing any credential."""
    cfg = current_app.config
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS") if not cfg.get(k)]
    return {"configured": not missing, "missing": missing,
            "host": cfg.get("IMAP_HOST"), "mailbox": cfg.get("IMAP_USER")}


def _decode(value):
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _plain_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    if msg.get_content_type() == "text/plain":
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _parse(raw):
    msg = message_from_bytes(raw)
    external_id = (msg.get("Message-ID") or "").strip()
    sender = _decode(msg.get("From"))
    subject = _decode(msg.get("Subject"))
    try:
        sent_at = parsedate_to_datetime(msg.get("Date"))
        if sent_at is not None:
            sent_at = sent_at.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        sent_at = None
    if not external_id:
        # No Message-ID: build a stable id so a re-sync does not duplicate the mail.
        external_id = "local-" + hashlib.sha256(
            f"{sender}|{subject}|{sent_at}".encode()).hexdigest()[:32]
    return {"external_id": external_id, "from": sender, "subject": subject,
            "body": _plain_text(msg)[:SNIPPET_CHARS], "created_at": sent_at or now()}


def sync(limit=FETCH_LIMIT):
    """Fetch new mail from the inbox and store it. Returns
    {"configured": bool, "synced": n, "error": str|None}. Never raises."""
    cfg = current_app.config
    if not status()["configured"]:
        return {"configured": False, "synced": 0, "error": None}
    try:
        mail = imaplib.IMAP4_SSL(cfg["IMAP_HOST"], int(cfg.get("IMAP_PORT") or 993), timeout=15)
        try:
            mail.login(cfg["IMAP_USER"], cfg["IMAP_PASS"])
            mail.select("INBOX")
            _typ, data = mail.search(None, "ALL")
            ids = (data[0] or b"").split()[-limit:]
            synced = 0
            for num in ids:
                _typ, fetched = mail.fetch(num, "(RFC822)")
                raw = fetched[0][1] if fetched and fetched[0] else None
                if not raw:
                    continue
                parsed = _parse(raw)
                if MailMessage.query.filter_by(external_id=parsed["external_id"]).first():
                    continue  # already stored: Message-ID dedupe
                db.session.add(MailMessage(direction="in", from_addr=parsed["from"][:150],
                                           to_addr=cfg.get("IMAP_USER"), subject=parsed["subject"][:200],
                                           body=parsed["body"], external_id=parsed["external_id"],
                                           source="inbox", created_at=parsed["created_at"]))
                synced += 1
            db.session.commit()
            return {"configured": True, "synced": synced, "error": None}
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    except Exception as exc:  # login failure, network failure: an honest error, not a crash
        db.session.rollback()
        current_app.logger.error("IMAP sync failed: %s", exc)
        return {"configured": True, "synced": 0, "error": str(exc)[:200]}
