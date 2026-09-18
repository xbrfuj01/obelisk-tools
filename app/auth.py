import os
import secrets
import threading
from datetime import datetime, timedelta

from passlib.context import CryptContext
from fastapi import Request
from sqlalchemy.orm import Session

from . import config
from .models import Setting, User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class NotAuthenticated(Exception):
    pass


class SiteNotAuthenticated(Exception):
    pass


def get_setting(db: Session, key: str, default=None):
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        row = Setting(key=key, value=value)
        db.add(row)
    db.commit()


def set_user_admin(db: Session, user_id: str, is_admin: bool) -> bool:
    """Grants or revokes admin rights on a site user. Refuses to revoke the
    last remaining admin, so the app can never lock itself out of /admin —
    there's no separate admin login to fall back on."""
    user = db.get(User, user_id)
    if not user:
        return False
    if not is_admin and user.is_admin:
        remaining = db.query(User).filter(User.is_admin.is_(True), User.id != user_id).count()
        if remaining == 0:
            return False
    user.is_admin = is_admin
    db.commit()
    return True


def ensure_secret_key(db: Session):
    if not get_setting(db, "secret_key"):
        set_setting(db, "secret_key", secrets.token_hex(32))


def get_secret_key(db: Session) -> str:
    return get_setting(db, "secret_key")


def get_retention_hours(db: Session) -> int:
    val = get_setting(db, "cleanup_hours")
    return int(val) if val else config.DEFAULT_CLEANUP_HOURS


def get_cleanup_interval_minutes(db: Session) -> int:
    val = get_setting(db, "cleanup_interval_minutes")
    return int(val) if val else config.DEFAULT_CLEANUP_INTERVAL_MINUTES


def get_max_concurrent_downloads(db: Session) -> int:
    val = get_setting(db, "max_concurrent_downloads")
    return int(val) if val else config.DEFAULT_MAX_CONCURRENT_DOWNLOADS


def get_max_concurrent_conversions(db: Session) -> int:
    val = get_setting(db, "max_concurrent_conversions")
    return int(val) if val else config.DEFAULT_MAX_CONCURRENT_CONVERSIONS


def get_max_upload_mb(db: Session) -> int:
    val = get_setting(db, "max_upload_mb")
    return int(val) if val else config.DEFAULT_MAX_UPLOAD_MB


def get_session_max_age_days(db: Session) -> int:
    val = get_setting(db, "session_max_age_days")
    return int(val) if val else config.DEFAULT_SESSION_MAX_AGE_DAYS


def get_proxy_url(db: Session) -> str:
    return get_setting(db, "proxy_url", "") or ""


def get_proxy_domains(db: Session) -> list:
    raw = get_setting(db, "proxy_domains", config.DEFAULT_PROXY_DOMAINS) or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def get_timezone(db: Session) -> str:
    return get_setting(db, "timezone", config.DEFAULT_TIMEZONE)


# ---------------- Cookies (for videos yt-dlp can't reach anonymously) ----------------
# Stored as a plain file (yt-dlp's cookiefile option reads Netscape-format
# cookies.txt directly) under DATA_DIR, which is the same persistent volume
# the database lives on - not the web-servable static dir, not the ephemeral
# downloads dir. Content is deliberately never read back into the admin UI
# once saved (write-only from the admin's point of view) since it's live
# access to whatever account it belongs to.

def _cookies_path() -> str:
    return os.path.join(config.DATA_DIR, "youtube_cookies.txt")


def has_cookies() -> bool:
    return os.path.exists(_cookies_path())


def get_cookies_path():
    path = _cookies_path()
    return path if os.path.exists(path) else None


def save_cookies(content: str):
    with open(_cookies_path(), "w", encoding="utf-8") as f:
        f.write(content)


def clear_cookies():
    try:
        os.remove(_cookies_path())
    except FileNotFoundError:
        pass


def is_admin_session(request: Request, db: Session) -> bool:
    """True if this session is logged into the site as a user with admin
    rights — the only way into /admin, there's no separate admin login."""
    username = request.session.get("site_username")
    if not username or not request.session.get("site_access"):
        return False
    user = db.query(User).filter(User.username == username).first()
    return bool(user and user.is_admin)


def require_admin(request: Request, db: Session):
    if not is_admin_session(request, db):
        raise NotAuthenticated()


# ---------------- Site-wide login gate (multi-user) ----------------
# Disabled until an admin creates at least one user from the admin panel —
# there is no env var seed for it (nothing to configure on deploy).

def is_site_gate_enabled(db: Session) -> bool:
    return db.query(User).count() > 0


def list_users(db: Session):
    return db.query(User).order_by(User.created_at).all()


def username_exists(db: Session, username: str) -> bool:
    return db.query(User).filter(User.username == username).first() is not None


def create_user(db: Session, username: str, password: str) -> User:
    user = User(username=username, password_hash=pwd_context.hash(password))
    db.add(user)
    db.commit()
    return user


def delete_user(db: Session, user_id: str) -> bool:
    """Refuses to delete the last remaining admin — same reasoning as
    set_user_admin: there's no separate admin login to fall back on."""
    user = db.get(User, user_id)
    if not user:
        return False
    if user.is_admin:
        remaining = db.query(User).filter(User.is_admin.is_(True), User.id != user_id).count()
        if remaining == 0:
            return False
    db.delete(user)
    db.commit()
    return True


def reset_user_password(db: Session, user_id: str, password: str) -> bool:
    user = db.get(User, user_id)
    if not user:
        return False
    user.password_hash = pwd_context.hash(password)
    db.commit()
    return True


def verify_site_credentials(db: Session, username: str, password: str) -> bool:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return False
    return pwd_context.verify(password, user.password_hash)


def record_login(db: Session, username: str):
    user = db.query(User).filter(User.username == username).first()
    if user:
        user.last_login = datetime.utcnow()
        db.commit()


# How often a logged-in user's activity timestamp actually gets written -
# most pages fire several requests a second (status polling etc.), so
# writing on every single one would be pure DB-write noise for a value
# that's only ever displayed rounded to a page reload anyway.
ACTIVITY_UPDATE_THROTTLE_SECONDS = 60

# A user counts as "online" if seen within this window. Wider than the
# throttle above so someone idling between polls doesn't flicker offline.
ONLINE_THRESHOLD_SECONDS = 180


def record_activity(db: Session, username: str | None):
    if not username:
        return
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return
    now = datetime.utcnow()
    if user.last_active and (now - user.last_active).total_seconds() < ACTIVITY_UPDATE_THROTTLE_SECONDS:
        return
    user.last_active = now
    db.commit()


def is_user_online(user: User) -> bool:
    if not user.last_active:
        return False
    return (datetime.utcnow() - user.last_active).total_seconds() < ONLINE_THRESHOLD_SECONDS


def require_site_access(request: Request, db: Session):
    if is_site_gate_enabled(db) and not request.session.get("site_access"):
        raise SiteNotAuthenticated()


# ---------------- Brute-force lockout ----------------

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

_login_lock = threading.Lock()
_login_attempts = {}  # key -> {"count": int, "locked_until": datetime | None}


def check_lockout(key: str):
    """Returns (is_locked, seconds_remaining)."""
    with _login_lock:
        entry = _login_attempts.get(key)
        if not entry or not entry.get("locked_until"):
            return False, 0
        remaining = (entry["locked_until"] - datetime.utcnow()).total_seconds()
        if remaining <= 0:
            _login_attempts.pop(key, None)
            return False, 0
        return True, int(remaining)


def register_failed_attempt(key: str):
    with _login_lock:
        entry = _login_attempts.setdefault(key, {"count": 0, "locked_until": None})
        entry["count"] += 1
        if entry["count"] >= MAX_LOGIN_ATTEMPTS:
            entry["locked_until"] = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)


def register_successful_attempt(key: str):
    with _login_lock:
        _login_attempts.pop(key, None)


# ---------------- Download rate limiting ----------------

DOWNLOAD_RATE_LIMIT = 20
DOWNLOAD_RATE_WINDOW_MINUTES = 10

_download_lock = threading.Lock()
_download_windows = {}  # key -> {"count": int, "window_started": datetime}


def check_download_rate_limit(key: str) -> bool:
    """Returns True if this key is allowed to submit another download now."""
    with _download_lock:
        entry = _download_windows.get(key)
        now = datetime.utcnow()
        if not entry or now - entry["window_started"] > timedelta(minutes=DOWNLOAD_RATE_WINDOW_MINUTES):
            _download_windows[key] = {"count": 1, "window_started": now}
            return True
        if entry["count"] >= DOWNLOAD_RATE_LIMIT:
            return False
        entry["count"] += 1
        return True
