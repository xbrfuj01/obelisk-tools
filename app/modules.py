import threading
import time

import httpx

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
    """Snapshot of every module's current availability, keyed the same way
    as config.MODULE_URLS - the shape templates/routes consult."""
    with _lock:
        return dict(_available)
