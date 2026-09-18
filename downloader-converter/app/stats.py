from datetime import datetime, timedelta

from sqlalchemy import func

from .models import Conversion, Download

UNKNOWN_USER = "— (без входу)"

PERIODS = [
    ("day", "24 г", timedelta(hours=24)),
    ("week", "7д", timedelta(days=7)),
    ("month", "30д", timedelta(days=30)),
    ("all", "весь час", None),
]


def _counts_by_user(db, model, since):
    q = db.query(
        model.username,
        func.count(model.id),
        func.coalesce(func.sum(model.filesize), 0),
    ).group_by(model.username)
    if since is not None:
        q = q.filter(model.created_at >= since)
    return {(row[0] or UNKNOWN_USER): (row[1], row[2]) for row in q.all()}


def user_activity(db):
    """Per-user download/conversion counts and total size for each of the
    rolling windows in PERIODS. Returns {period_key: [rows...]}, rows sorted
    by total activity descending."""
    now = datetime.utcnow()
    result = {}
    for key, _label, delta in PERIODS:
        since = now - delta if delta else None
        dl = _counts_by_user(db, Download, since)
        cv = _counts_by_user(db, Conversion, since)
        rows = []
        for username in set(dl) | set(cv):
            d_count, d_size = dl.get(username, (0, 0))
            c_count, c_size = cv.get(username, (0, 0))
            rows.append({
                "username": username,
                "downloads": d_count,
                "conversions": c_count,
                "size": d_size + c_size,
            })
        rows.sort(key=lambda r: (r["downloads"] + r["conversions"]), reverse=True)
        result[key] = rows
    return result
