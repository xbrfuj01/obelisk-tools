import threading
import time

import httpx
from fastapi import Request
from sqlalchemy.orm import Session

from . import auth, config
from .database import SessionLocal

# How long a cached health result is trusted before the next background
# recheck - short enough that a module coming up/down is reflected quickly
# in the hub/admin UI, long enough that a page load never blocks on a live
# network round-trip to every module.
_RECHECK_INTERVAL_SECONDS = 20
_CHECK_TIMEOUT_SECONDS = 3

_lock = threading.Lock()
# Optimistic default (True) until the first check completes, so a module
# that's actually up isn't hidden for the few seconds before the background
# loop's first tick - a genuinely down module just takes one tick to hide.
_available = {name: True for name in config.MODULE_URLS}

# Admin on/off switch (auth.get/set_module_enabled) - separate from health
# above and from the DB: an admin route flips this dict directly the
# moment it saves, so every other request sees it immediately without a
# DB round-trip. Loaded from the DB once at startup (load_enabled_state).
_enabled = {name: True for name in config.MODULE_URLS}


def _check_once(name: str, base_url: str) -> bool:
    try:
        resp = httpx.get(f"{base_url}/health", timeout=_CHECK_TIMEOUT_SECONDS)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def push_downloader_converter_config():
    """The downloader+converter module has no Setting table of its own -
    core owns the real values and pushes them here. Called after every
    admin settings save, plus opportunistically on each health-check tick
    below so the module self-heals its in-memory cache after a restart
    without needing retry/backoff logic tied to a specific admin action."""
    db = SessionLocal()
    try:
        data = {
            "max_concurrent_downloads": auth.get_max_concurrent_downloads(db),
            "max_concurrent_conversions": auth.get_max_concurrent_conversions(db),
            "proxy_url": auth.get_proxy_url(db),
            "proxy_domains": auth.get_proxy_domains(db),
            "youtube_proxy_url": auth.get_youtube_proxy_url(db),
            "retention_hours": auth.get_retention_hours(db),
            "cleanup_interval_minutes": auth.get_cleanup_interval_minutes(db),
        }
    finally:
        db.close()
    try:
        httpx.post(f"{config.DOWNLOADER_CONVERTER_URL}/internal/config", json=data, timeout=5)
    except httpx.HTTPError:
        pass


def _loop():
    while True:
        for name, base_url in config.MODULE_URLS.items():
            ok = _check_once(name, base_url)
            with _lock:
                _available[name] = ok
            if name == "downloader_converter" and ok:
                push_downloader_converter_config()
        time.sleep(_RECHECK_INTERVAL_SECONDS)


def start_health_check_thread():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def is_module_available(name: str) -> bool:
    with _lock:
        return _available.get(name, False)


def available_modules() -> dict:
    """Snapshot of every module's current health, keyed the same way as
    config.MODULE_URLS - unaffected by the admin on/off switch below."""
    with _lock:
        return dict(_available)


def load_enabled_state(db: Session):
    """Reads the admin on/off switch from the DB into the in-memory cache -
    called once at startup. Nothing else needs to re-read the DB for this:
    the only writer afterwards is the admin route that flips it, and that
    updates the cache directly (set_module_enabled)."""
    global _enabled
    with _lock:
        _enabled = {name: auth.get_module_enabled(db, name) for name in config.MODULE_URLS}


def set_module_enabled(name: str, enabled: bool):
    with _lock:
        _enabled[name] = enabled


def is_module_enabled(name: str) -> bool:
    with _lock:
        return _enabled.get(name, True)


def is_enabled_for(request: Request, db: Session, name: str) -> bool:
    """Whether `name`'s admin switch should block this particular request -
    True whenever the switch is on, and also for an admin's own session
    even while it's off, so an admin can still open/manage a module while
    it's hidden from everyone else. Independent of is_module_available
    (container health) - a module can be perfectly reachable and still be
    off by policy; callers check both separately since the two failure
    reasons are worth keeping distinct in the code even if they look the
    same to the person who hit "Модуль недоступний"."""
    if is_module_enabled(name):
        return True
    return auth.is_admin_session(request, db)
