import threading
import time

import httpx

from . import config

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


def _loop():
    while True:
        for name, base_url in config.MODULE_URLS.items():
            ok = _check_once(name, base_url)
            with _lock:
                _available[name] = ok
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
