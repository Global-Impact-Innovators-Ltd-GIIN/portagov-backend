"""Announcements: public notices from the platform or a ministry.

GET /api/announcements is public and newest first. POST and DELETE are
admin only (staff token). The console Announcements section, the
citizen home strip, the announcement board and the emergency banner all
read from these endpoints, so a notice posted once reaches every
device, and a wrong or stale notice can be taken down.
"""
from flask import Blueprint, jsonify, request

from ..extensions import db
from ..models import Announcement
from ..utils import audit, auth_required, err, rate_limit

bp = Blueprint("announcements", __name__, url_prefix="/api")


@bp.get("/announcements")
def list_announcements():
    ministry = (request.args.get("ministry") or "").strip()
    try:
        limit = int(request.args.get("limit") or 20)
    except ValueError:
        return err("limit must be a number")
    limit = max(1, min(limit, 50))
    q = Announcement.query.order_by(Announcement.id.desc())
    if ministry:
        q = q.filter(Announcement.ministry == ministry)
    rows = q.limit(limit).all()
    return jsonify({"announcements": [r.to_dict() for r in rows]})


@bp.post("/admin/announcements")
@auth_required("admin")
@rate_limit("announcement_post", 20, 3600)
def post_announcement():
    d = request.get_json(silent=True) or {}
    ministry = (d.get("ministry") or "").strip()
    title = (d.get("title") or "").strip()
    body = (d.get("body") or "").strip()
    if len(ministry) < 2 or len(ministry) > 120:
        return err("Ministry must be 2 to 120 characters")
    if len(title) < 3 or len(title) > 160:
        return err("Title must be 3 to 160 characters")
    if len(body) < 5 or len(body) > 1000:
        return err("Body must be 5 to 1000 characters")
    row = Announcement(ministry=ministry, title=title, body=body,
                       urgent=bool(d.get("urgent", False)))
    db.session.add(row)
    audit("announcement_post", f"{ministry}: {title}")
    db.session.commit()
    return jsonify(row.to_dict()), 201


@bp.delete("/admin/announcements/<int:ann_id>")
@auth_required("admin")
def delete_announcement(ann_id):
    row = db.session.get(Announcement, ann_id)
    if not row:
        return err("Announcement not found", 404)
    audit("announcement_delete", f"{row.ministry}: {row.title}")
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})
