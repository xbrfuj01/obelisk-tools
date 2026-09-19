"""In-memory mirror of the admin-configured settings this module needs,
kept current by core pushing to POST /internal/config - see main.py. Core
owns the real Setting table (this module has no settings of its own); this
is just a cache that self-heals on the next push after a restart, with
sane defaults until the first one arrives."""

import threading

_lock = threading.Lock()
_settings = {
    "max_concurrent_downloads": 2,
    "max_concurrent_conversions": 1,
    "proxy_url": "",
    "proxy_domains": [],
    # Separate from proxy_url above on purpose: a proxy that works for the
    # generic "blocked sites" list (vk/ok/rutube) doesn't necessarily get
    # past YouTube's much stricter bot detection, and vice versa - sharing
    # one address means fixing one can quietly break the other. Only ever
    # used reactively (see downloader.py's geo-block retry), never
    # up-front, so a normal video's speed is unaffected either way.
    "youtube_proxy_url": "",
    "retention_hours": 24,
    "cleanup_interval_minutes": 30,
}


def update(data: dict):
    with _lock:
        _settings.update(data)


def get(key, default=None):
    with _lock:
        return _settings.get(key, default)
