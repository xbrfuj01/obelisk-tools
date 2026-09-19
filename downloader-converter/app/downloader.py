import ipaddress
import os
import re
import shutil
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlparse

import yt_dlp

from . import config, settings_store
from .database import SessionLocal
from .models import Download

# Confirmed via a real container log (2026-09-15): without a PO token,
# tv_simply's own formats get skipped outright ("client https formats
# require a GVS PO Token which was not provided"), "tv" was UNPLAYABLE for
# the test video, and web/ios both got SABR-forced - zero usable formats
# from any client. A PO token isn't optional complexity from the earlier
# SABR era, it's YouTube's current baseline requirement regardless of
# client. bgutil-provider (docker-compose.yml sidecar) generates one via
# the youtubepot-bgutilhttp extractor.
#
# Values must be lists, matching how --extractor-args "key=value" gets
# parsed on the CLI ({'base_url': ['http://...']}) - a bare string here
# gets iterated character-by-character instead.
#
# player_client is deliberately NOT listed here (see
# _extract_youtube_client_priority below) - passing several clients in one
# extractor_args list makes yt-dlp query every single one of them (each its
# own webpage/player/PO-token round trip) before returning, even once an
# earlier client already found perfectly good formats. Trying them one at a
# time and stopping at the first success is the same eventual result for
# the common case (some client works) at a fraction of the latency.
YOUTUBE_EXTRACTOR_ARGS = {
    "youtubepot-bgutilhttp": {"base_url": ["http://bgutil-provider:4416"]},
}

# "tv_simply" (TVHTML5_SIMPLY, yt-dlp/yt-dlp#13389) - a real captured
# videoplayback URL from a third-party downloader site showed this client
# handing out a plain, direct, non-SABR HTTPS URL (known Content-Length) at
# very high quality (itag 337, 2160p60), apparently not swept up in
# YouTube's SABR-forcing rollout the way "web" has been - but it still
# needs the same PO token as everything else. Tried first for that reason;
# web/tv/ios are the fallback for whatever tv_simply doesn't cover (a real
# log showed tv_simply skipped outright for a video "tv"/"web" still
# handled). ios/tv_simply don't support cookie-authenticated requests at
# all (yt-dlp skips them outright rather than erroring), so the
# cookie-retry pass only tries the two clients that actually accept cookies.
YOUTUBE_CLIENT_PRIORITY_ANON = ["tv_simply", "web", "tv", "ios"]
YOUTUBE_CLIENT_PRIORITY_COOKIES = ["web", "tv"]

# Short-lived memory of which YouTube client (and whether cookies were
# needed) actually worked for a given URL. probe_qualities (when the user
# pastes a link) and the real download (when they click "Завантажити") are
# two entirely separate extract_info calls seconds-to-minutes apart - the
# probe's actual format URLs can't be reused for the real download (they're
# short-lived, and the download needs its own progress-hooked yt-dlp
# instance anyway) - but *which client to even bother asking first* is
# almost always still the same answer, so this lets the real download skip
# straight to the client that already worked instead of re-running the
# whole tv_simply -> web -> tv -> ios search from scratch.
_youtube_client_cache = {}
_youtube_client_cache_lock = threading.Lock()
_YOUTUBE_CLIENT_CACHE_TTL = timedelta(minutes=10)


def _cache_youtube_client(url: str, client: str, used_cookies: bool):
    with _youtube_client_cache_lock:
        _youtube_client_cache[url] = (client, used_cookies, datetime.utcnow() + _YOUTUBE_CLIENT_CACHE_TTL)


def _get_cached_youtube_client(url: str):
    """Returns (client, used_cookies) if a not-yet-expired hint exists for
    this exact URL, else None."""
    with _youtube_client_cache_lock:
        entry = _youtube_client_cache.get(url)
        if not entry:
            return None
        client, used_cookies, expires = entry
        if datetime.utcnow() > expires:
            del _youtube_client_cache[url]
            return None
        return client, used_cookies


def _extract_youtube_client_priority(ydl_opts, url, download, clients, cached_client=None, before_retry=None):
    """Tries each client in order, stopping at the first that actually
    yields usable formats - yt-dlp's own per-client "https formats have
    been skipped" warnings already mean a client with zero returned
    formats is a dead end for this video, not something to fall back into
    later. cached_client (see _youtube_client_cache above), if given and
    present in `clients`, jumps the queue to be tried first regardless of
    the normal priority order. before_retry, when downloading, cleans up
    whatever a failed client's own partial download may have left behind
    before the next client's attempt starts (skipped on the very first
    attempt - nothing to clean up yet)."""
    ordered = clients
    if cached_client and cached_client in clients:
        ordered = [cached_client] + [c for c in clients if c != cached_client]
    last_error = None
    for i, client in enumerate(ordered):
        if i > 0 and before_retry:
            before_retry()
        opts = dict(ydl_opts)
        extractor_args = dict(opts.get("extractor_args") or {})
        extractor_args["youtube"] = {**extractor_args.get("youtube", {}), "player_client": [client]}
        opts["extractor_args"] = extractor_args
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
        except Exception as e:
            last_error = e
            continue
        if info and info.get("formats"):
            return info, client
        last_error = RuntimeError(f'YouTube client "{client}" yielded no usable formats')
    raise last_error


def _is_youtube_url(url: str) -> bool:
    return _source_from_url(url) in ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com")


# Sized generously and fixed — the actual concurrency cap is admin-configurable
# (max_concurrent_downloads, stored in the DB) and enforced by _ConcurrencyGate
# below, not by this pool's size.
_executor = ThreadPoolExecutor(max_workers=16)

COMMON_LABELS = {
    4320: "8K",
    2160: "4K",
    1440: "2K",
    1080: "Full HD",
    720: "HD",
}

SKIP_EXT = {".srt", ".vtt", ".json", ".description", ".part", ".ytdl"}

# How much each new speed sample moves the smoothed estimate used for ETA -
# lower means steadier (slower to react to a real speed change), higher
# means more responsive (but jitterier). 0.25 roughly averages the last few
# progress-hook callbacks, which fire every second or so.
ETA_SMOOTHING_ALPHA = 0.25

LANG_NAMES = {
    # Native/autonym names for languages Ukrainian users are most likely to
    # see; everything else falls back to an English name below rather than
    # a raw 2-3 letter code — YouTube alone offers 100+ auto-translated
    # caption languages, most of which will never hit the first list.
    "uk": "Українська", "en": "English", "ru": "Русский", "es": "Español",
    "fr": "Français", "de": "Deutsch", "pl": "Polski", "pt": "Português",
    "it": "Italiano", "ja": "日本語", "ko": "한국어", "zh": "中文", "tr": "Türkçe",
    "ar": "العربية", "hi": "हिन्दी", "id": "Bahasa Indonesia", "vi": "Tiếng Việt",
    "th": "ไทย", "nl": "Nederlands", "sv": "Svenska", "no": "Norsk", "da": "Dansk",
    "fi": "Suomi", "cs": "Čeština", "sk": "Slovenčina", "hu": "Magyar",
    "ro": "Română", "bg": "Български", "el": "Ελληνικά", "he": "עברית", "iw": "עברית",
    "fa": "فارسی", "ur": "اردو", "bn": "বাংলা", "ta": "தமிழ்", "sr": "Српски",
    "hr": "Hrvatski", "sl": "Slovenščina", "lt": "Lietuvių", "lv": "Latviešu",
    "et": "Eesti", "be": "Беларуская", "ka": "ქართული", "hy": "Հայերեն",
    "az": "Azərbaycan", "kk": "Қазақша", "uz": "Oʻzbekcha", "ms": "Bahasa Melayu",
    "sw": "Kiswahili", "fil": "Filipino", "sq": "Shqip", "mk": "Македонски",

    "aa": "Afar", "ab": "Abkhaz", "ae": "Avestan", "af": "Afrikaans",
    "ak": "Akan", "am": "Amharic", "an": "Aragonese", "as": "Assamese",
    "av": "Avar", "ay": "Aymara", "ba": "Bashkir", "bh": "Bihari",
    "bi": "Bislama", "bm": "Bambara", "bo": "Tibetan", "br": "Breton",
    "bs": "Bosnian", "ca": "Catalan", "ce": "Chechen", "ch": "Chamorro",
    "co": "Corsican", "cr": "Cree", "cu": "Church Slavic", "cv": "Chuvash",
    "cy": "Welsh", "dv": "Divehi", "dz": "Dzongkha", "ee": "Ewe",
    "eo": "Esperanto", "eu": "Basque", "ff": "Fulah", "fj": "Fijian",
    "fo": "Faroese", "fy": "Western Frisian", "ga": "Irish",
    "gd": "Scottish Gaelic", "gl": "Galician", "gn": "Guarani",
    "gu": "Gujarati", "gv": "Manx", "ha": "Hausa", "haw": "Hawaiian",
    "hmn": "Hmong", "ho": "Hiri Motu", "hz": "Herero", "ia": "Interlingua",
    "ie": "Interlingue", "ig": "Igbo", "ii": "Sichuan Yi", "ik": "Inupiaq",
    "io": "Ido", "is": "Icelandic", "iu": "Inuktitut", "jv": "Javanese",
    "jw": "Javanese", "kg": "Kongo", "ki": "Kikuyu", "kj": "Kuanyama",
    "kl": "Kalaallisut", "km": "Khmer", "kn": "Kannada", "kr": "Kanuri",
    "ks": "Kashmiri", "ku": "Kurdish", "kv": "Komi", "kw": "Cornish",
    "ky": "Kyrgyz", "la": "Latin", "lb": "Luxembourgish", "lg": "Ganda",
    "li": "Limburgish", "ln": "Lingala", "lo": "Lao", "lu": "Luba-Katanga",
    "mg": "Malagasy", "mh": "Marshallese", "mi": "Maori", "ml": "Malayalam",
    "mn": "Mongolian", "mr": "Marathi", "mt": "Maltese", "my": "Burmese",
    "na": "Nauru", "nb": "Norwegian Bokmål", "nd": "North Ndebele",
    "ne": "Nepali", "ng": "Ndonga", "nn": "Norwegian Nynorsk",
    "nr": "South Ndebele", "nv": "Navajo", "ny": "Chichewa", "oc": "Occitan",
    "oj": "Ojibwe", "om": "Oromo", "or": "Odia", "os": "Ossetian",
    "pa": "Punjabi", "pi": "Pali", "ps": "Pashto", "qu": "Quechua",
    "rm": "Romansh", "rn": "Rundi", "rw": "Kinyarwanda", "sa": "Sanskrit",
    "sc": "Sardinian", "sd": "Sindhi", "se": "Northern Sami", "sg": "Sango",
    "si": "Sinhala", "sm": "Samoan", "sn": "Shona", "so": "Somali",
    "ss": "Swati", "st": "Southern Sotho", "su": "Sundanese", "te": "Telugu",
    "tg": "Tajik", "ti": "Tigrinya", "tk": "Turkmen", "tl": "Tagalog",
    "tn": "Tswana", "to": "Tongan", "ts": "Tsonga", "tt": "Tatar",
    "tw": "Twi", "ty": "Tahitian", "ug": "Uyghur", "ve": "Venda",
    "vo": "Volapük", "wa": "Walloon", "wo": "Wolof", "xh": "Xhosa",
    "yi": "Yiddish", "yo": "Yoruba", "za": "Zhuang", "zu": "Zulu",

    # YouTube-specific/legacy codes not in plain ISO 639-1 (its caption
    # auto-translate list includes languages with no 2-letter code at all).
    "bho": "Bhojpuri", "ceb": "Cebuano", "kri": "Krio", "luo": "Luo",
    "nso": "Northern Sotho", "yue": "Cantonese", "in": "Bahasa Indonesia",
    "ji": "Yiddish", "mni": "Meitei", "gom": "Konkani", "doi": "Dogri",
}

PRIORITY_LANG_ORDER = ["uk", "en", "ru"]


def _lang_label(code):
    return LANG_NAMES.get(code) or LANG_NAMES.get(code.split("-")[0]) or code


EMBEDDABLE_SUBTITLE_CONTAINERS = {"mp4", "mkv"}

VIDEO_FORMATS = {"mp4", "webm", "mkv"}
AUDIO_FORMATS = {"mp3", "m4a", "opus", "wav"}


class _ConcurrencyGate:
    """Caps how many downloads actually run at once, against a limit that
    can change at runtime (read fresh from the DB on every acquire)."""

    def __init__(self):
        self._cv = threading.Condition()
        self._active = 0

    def acquire(self, limit: int):
        with self._cv:
            while self._active >= limit:
                self._cv.wait()
            self._active += 1

    def release(self):
        with self._cv:
            self._active -= 1
            self._cv.notify()


_gate = _ConcurrencyGate()

# job_ids the user has asked to cancel. A queued job checks this right after
# acquiring its concurrency slot (and skips running yt-dlp at all); an
# in-progress one is caught by the progress hook, which fires often enough
# for this to feel roughly instant.
_cancel_requested = set()


def request_cancel(job_id: str):
    _cancel_requested.add(job_id)


def clear_ytdlp_cache():
    """Wipes yt-dlp's own on-disk cache (extractor artifacts, nsig functions, ...).
    A common fix when extraction starts failing for reasons unrelated to our code."""
    yt_dlp.YoutubeDL({"quiet": True}).cache.remove()


def parse_timecode(value):
    """Accepts "SS", "MM:SS" or "HH:MM:SS" (fractional seconds allowed) and
    returns seconds as a float, or None if empty/unparseable/out of range.

    Only the leftmost (most significant) part is unbounded — minutes and
    seconds parts must each be below 60, so "00:61:67" is rejected rather
    than silently normalized.
    """
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) > 3:
        return None
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    if any(p < 0 for p in parts):
        return None
    if any(p >= 60 for p in parts[1:]):
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def _height_filter(quality: str) -> str:
    if not quality or quality == "best":
        return ""
    try:
        height = int(quality)
    except ValueError:
        return ""
    return f"[height<={height}]"


def _subtitle_options(info):
    manual = (info or {}).get("subtitles") or {}
    auto = (info or {}).get("automatic_captions") or {}
    result = []
    seen_bases = set()
    for code in manual:
        base = code.split("-")[0]
        if base in seen_bases:
            continue
        seen_bases.add(base)
        result.append({"code": code, "label": _lang_label(code), "auto": False})
    for code in auto:
        base = code.split("-")[0]
        if base in seen_bases:
            continue
        seen_bases.add(base)
        result.append({"code": code, "label": _lang_label(code), "auto": True})

    def sort_key(item):
        base = item["code"].split("-")[0]
        if base in PRIORITY_LANG_ORDER:
            return (0, PRIORITY_LANG_ORDER.index(base))
        return (1, 0)

    result.sort(key=sort_key)
    return result


# Substrings of the actual error text yt-dlp raises when a video is
# blocked specifically for the requester's own country - as opposed to any
# other extraction failure, which retrying through the proxy wouldn't fix
# and would just add a pointless second round-trip before the real error.
# Matched on the message itself rather than a specific exception class,
# since different extractors (and different yt-dlp versions) wrap the same
# underlying reason in different exception types.
_GEO_BLOCK_MARKERS = (
    "country domain due to a legal complaint",
    "not available in your country",
    "not made this video available in your country",
    "blocked it in your country",
)


def _is_geo_block_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _GEO_BLOCK_MARKERS)


def _extract_with_cookie_fallback(ydl_opts, url, *, download, should_retry=lambda: True, before_retry=None):
    """Thin wrapper around _extract_with_cookie_fallback_attempt that adds
    exactly one more retry - through the configured proxy - but only when
    the attempt above failed with a geo-block error (_is_geo_block_error)
    and wasn't already going through the proxy. A successful video never
    reaches this except branch at all, so this can't add latency to the
    common case; a video that fails for any other reason (private, deleted,
    network error) still fails on the first try, since a proxy wouldn't
    change that outcome."""
    try:
        return _extract_with_cookie_fallback_attempt(
            ydl_opts, url, download=download, should_retry=should_retry, before_retry=before_retry,
        )
    except Exception as e:
        proxy_url = settings_store.get("proxy_url", "")
        if not proxy_url or ydl_opts.get("proxy") or not should_retry() or not _is_geo_block_error(e):
            raise
        if before_retry:
            before_retry()
        proxied_opts = dict(ydl_opts)
        proxied_opts["proxy"] = proxy_url
        return _extract_with_cookie_fallback_attempt(
            proxied_opts, url, download=download, should_retry=should_retry, before_retry=before_retry,
        )


def _extract_with_cookie_fallback_attempt(ydl_opts, url, *, download, should_retry=lambda: True, before_retry=None):
    """Tries anonymously first - most videos don't need an authenticated
    session, and the cookies belong to one specific account that's better
    exercised sparingly than spent on every single request. Only retries
    with cookies if the anonymous attempt actually fails, and only when
    should_retry() still allows it (e.g. not for a job that was cancelled
    mid-flight, which isn't a real failure to retry).

    For YouTube specifically, each of the two passes (anonymous/cookies)
    goes through _extract_youtube_client_priority instead of a single
    extract_info call - see that function and YOUTUBE_CLIENT_PRIORITY_ANON/
    _COOKIES above for why.

    Returns (info, used_cookies) - callers that don't care which path
    succeeded (e.g. probing) can just discard the second value."""
    is_youtube = _is_youtube_url(url)
    cached = _get_cached_youtube_client(url) if is_youtube else None
    try:
        if is_youtube:
            cached_client = cached[0] if cached and not cached[1] else None
            info, client = _extract_youtube_client_priority(
                ydl_opts, url, download, YOUTUBE_CLIENT_PRIORITY_ANON, cached_client=cached_client, before_retry=before_retry,
            )
            _cache_youtube_client(url, client, False)
        else:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=download)
        return info, False
    except Exception:
        cookies_path = config.get_cookies_path()
        if not cookies_path or ydl_opts.get("cookiefile") or not should_retry():
            raise
        if before_retry:
            before_retry()
        retry_opts = dict(ydl_opts)
        retry_opts["cookiefile"] = cookies_path
        try:
            if is_youtube:
                cached_client = cached[0] if cached and cached[1] else None
                info, client = _extract_youtube_client_priority(
                    retry_opts, url, download, YOUTUBE_CLIENT_PRIORITY_COOKIES, cached_client=cached_client, before_retry=before_retry,
                )
                _cache_youtube_client(url, client, True)
            else:
                with yt_dlp.YoutubeDL(retry_opts) as ydl:
                    info = ydl.extract_info(url, download=download)
            return info, True
        finally:
            # get_cookies_path() hands out a fresh throwaway copy per call
            # (see its own docstring for why) - this module's own job to
            # clean up, since core's cookies.txt itself is never touched.
            try:
                os.remove(cookies_path)
            except OSError:
                pass


def probe_qualities(url: str):
    """Fetch the real (width x height) resolutions and subtitle languages available for this URL."""
    if not is_url_allowed(url):
        raise RuntimeError("Це посилання вказує на заборонену адресу")
    ydl_opts = {
        "quiet": True,
        # See the matching comment in _run_job's ydl_opts - needed for the
        # real client-by-client failure reason to reach docker logs at all.
        "no_warnings": False,
        "verbose": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
    }
    if _should_use_proxy(url):
        ydl_opts["proxy"] = settings_store.get("proxy_url", "")
    info, _ = _extract_with_cookie_fallback(ydl_opts, url, download=False)

    # dedupe by height only: several formats (different codecs/bitrates) often
    # share the same height, and the download-side quality filter also caps by height
    by_height = {}
    best_audio_bytes = None
    for f in (info or {}).get("formats", []) or []:
        h = f.get("height")
        w = f.get("width")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        # Deliberately not estimated from tbr when missing: bitrate isn't a
        # reliable stand-in for actual size (it skewed badly high on real
        # videos in practice), and a blank size beats a wrong one.
        fsize = f.get("filesize") or f.get("filesize_approx")

        if h and vcodec not in (None, "none"):
            # yt-dlp's own "bestvideo"/"bestaudio" selectors pick the LAST
            # matching entry in this list — it comes back pre-sorted
            # worst-to-best by yt-dlp's own preference — so mirror that
            # instead of picking whichever codec variant happens to have the
            # biggest file at this height. Otherwise the shown estimate can
            # be a completely different (larger) codec than what a real
            # download actually selects.
            by_height[int(h)] = {"width": w, "bytes": fsize}
        elif (not h) and vcodec in (None, "none") and acodec not in (None, "none"):
            best_audio_bytes = fsize

    result = []
    for h in sorted(by_height, reverse=True):
        entry = by_height[h]
        w = entry["width"]
        label = f"{w}×{h}" if w else f"{h}p"
        common = COMMON_LABELS.get(h)
        if common:
            label += f" ({common})"
        result.append({
            "value": str(h),
            "label": label,
            "video_bytes": entry["bytes"],
            "audio_bytes": best_audio_bytes or None,
        })
    return {"qualities": result, "subtitles": _subtitle_options(info)}


def _source_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", netloc)
    except Exception:
        return "unknown"


def _should_use_proxy(url: str) -> bool:
    proxy_url = settings_store.get("proxy_url", "")
    if not proxy_url:
        return False
    domains = settings_store.get("proxy_domains", [])
    if not domains:
        return True
    source = _source_from_url(url)
    return any(d in source for d in domains)


def check_proxy_connection(proxy_url: str, timeout: float = 6.0) -> bool:
    """Quick end-to-end reachability check for the "Проксі для заблокованих
    сайтів" admin setting - routed through yt-dlp's own request machinery
    (the same SOCKS/TLS path a real download would use) rather than a raw
    socket check, so it actually proves traffic gets through. Targets a
    small, unrelated, always-up host instead of one of the actual blocked
    sites, so the result reflects the proxy itself, not that site's own
    uptime or anti-bot behavior."""
    if not proxy_url:
        return False
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "proxy": proxy_url, "socket_timeout": timeout}) as ydl:
            ydl.urlopen("https://api.ipify.org").read()
        return True
    except Exception:
        return False


def _is_safe_direct_url(url: str) -> bool:
    """Rejects hosts that resolve to a private/internal IP, so the download
    form can't be used to make the server probe its own local network."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        for info in socket.getaddrinfo(hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def is_url_allowed(url: str) -> bool:
    # Proxied domains are a small admin-curated allowlist (not attacker
    # controlled) and are resolved remotely by the proxy anyway, so the
    # local SSRF check doesn't apply to them.
    if _should_use_proxy(url):
        return True
    return _is_safe_direct_url(url)


def submit_job(job_id: str):
    _executor.submit(_run_job, job_id)


def _update(db, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()


def _find_main_file(out_dir: str):
    candidates = []
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() not in SKIP_EXT:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)


def _combine_leg_progress(leg: int, leg_pct: float) -> float:
    """Maps progress within one download leg to a spot on the overall bar.

    "video" mode downloads video and audio as two separate legs (yt-dlp
    merges them afterwards via ffmpeg), which otherwise shows as the bar
    going 0->100% twice. The first leg (video, normally the bulk of the
    size) gets 0-90%, a second leg (audio) gets 90-99%, and the last 1% is
    left for the merge/postprocessing step, which isn't itself tracked here.
    """
    if leg <= 1:
        return round(leg_pct * 0.9, 1)
    if leg == 2:
        return round(90 + leg_pct * 0.09, 1)
    return 99.0


def _progress_hook(job_id, d, state):
    if job_id in _cancel_requested:
        # yt_dlp.utils.DownloadCancelled is the library's own supported way
        # to abort mid-download from inside a progress hook - unlike a plain
        # exception, it's guaranteed not to get swallowed as "a hook errored,
        # ignoring it" and actually propagates out of extract_info().
        from yt_dlp.utils import DownloadCancelled
        raise DownloadCancelled("cancelled by user")
    db = SessionLocal()
    try:
        job = db.get(Download, job_id)
        if not job:
            return
        status = d.get("status")
        if status == "downloading":
            if not state["leg_active"]:
                state["leg_active"] = True
                state["leg"] += 1
                state["smoothed_speed"] = None  # new leg (e.g. audio after video) - unrelated transfer, fresh start
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            # For timecode-clipped downloads yt-dlp's ffmpeg-based range
            # downloader never fires this hook mid-download at all (only
            # once, at "finished") — so total/downloaded stay at 0 here and
            # the UI shows an indeterminate bar instead of a fake percentage.
            leg_pct = (downloaded / total * 100) if total else 0
            job.progress = _combine_leg_progress(state["leg"], leg_pct)
            job.status = "downloading"

            # yt-dlp's own eta jumps around a lot (it's derived from a very
            # short recent window - a momentary speed dip reads as "3 годин"
            # one tick and "5 хв" the next). Smoothing the transfer rate
            # ourselves with an exponential moving average - each new sample
            # nudges the estimate instead of replacing it outright - reacts
            # to a real, sustained speed change without visibly jittering.
            raw_speed = d.get("speed")
            if raw_speed:
                prev = state.get("smoothed_speed")
                state["smoothed_speed"] = raw_speed if not prev else (
                    ETA_SMOOTHING_ALPHA * raw_speed + (1 - ETA_SMOOTHING_ALPHA) * prev
                )
            smoothed_speed = state.get("smoothed_speed")
            if total and smoothed_speed:
                job.eta_seconds = max(0, round((total - downloaded) / smoothed_speed))
            else:
                job.eta_seconds = None
            db.commit()
        elif status == "finished":
            state["leg_active"] = False
            job.progress = _combine_leg_progress(state["leg"], 100)
            job.eta_seconds = None
            db.commit()
    except Exception:
        pass
    finally:
        db.close()


def _run_job(job_id: str):
    db = SessionLocal()
    job = db.get(Download, job_id)
    if not job:
        db.close()
        return

    limit = settings_store.get("max_concurrent_downloads", 2)
    _gate.acquire(limit)
    try:
        if job_id in _cancel_requested:
            # Cancelled while it was still waiting for a concurrency slot -
            # never actually started, so there's nothing to abort mid-flight.
            _update(db, job, status="cancelled", eta_seconds=None, finished_at=datetime.utcnow())
            return

        if not is_url_allowed(job.url):
            raise RuntimeError("Це посилання вказує на заборонену адресу")

        _update(db, job, status="downloading")

        out_dir = os.path.join(config.DOWNLOAD_DIR, job_id)
        os.makedirs(out_dir, exist_ok=True)
        outtmpl = os.path.join(out_dir, "%(title).150B.%(ext)s")

        height_filter = _height_filter(job.quality)

        progress_state = {"leg": 0, "leg_active": False}
        ydl_opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            # False/True (not the usual True/False) so yt-dlp's own
            # [debug]/warning trail actually reaches the container's stdout
            # instead of vanishing - with no custom "logger" set, this is
            # the only way to see *which* client failed and why on a real
            # failure like "The page needs to be reloaded" (yt-dlp's own
            # generic message once every client comes up empty).
            "no_warnings": False,
            "verbose": True,
            "progress_hooks": [lambda d: _progress_hook(job_id, d, progress_state)],
            "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
        }
        if _should_use_proxy(job.url):
            ydl_opts["proxy"] = settings_store.get("proxy_url", "")

        if job.clip_start is not None or job.clip_end is not None:
            from yt_dlp.utils import download_range_func
            start = job.clip_start or 0
            end = job.clip_end if job.clip_end is not None else float("inf")
            ydl_opts["download_ranges"] = download_range_func([], [(start, end)])
            # Without this, ffmpeg trims via stream copy, which requires an
            # actual keyframe inside the requested range to cut on. Short
            # clips (YouTube Shorts, or a tight timecode range on a longer
            # video) often have only one keyframe for the whole clip, so a
            # stream-copy cut either fails outright ("ffmpeg exited with
            # code ...") or silently produces a near-empty file. Re-encoding
            # is slower but always produces a correct, complete clip.
            ydl_opts["force_keyframes_at_cuts"] = True

        if job.mode == "audio":
            audio_codec = job.container if job.container in AUDIO_FORMATS else "mp3"
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_codec,
                "preferredquality": "192",
            }]
        elif job.mode == "video_only":
            base_format = f"bestvideo{height_filter}/best{height_filter}"
            # yt-dlp's own default codec ranking places vp9/av1 above h264
            # at equal resolution (they're the better-quality-per-bit
            # choice), so a plain "bestvideo" almost always hands back a
            # vp9/av1 stream even on videos where an h264 stream exists at
            # the exact same height - premiere_compat then has to re-encode
            # it after the fact (see the premiere_compat block after the
            # download) for a resolution where that was never necessary.
            # [vcodec^=avc1] asks for h264 first at the same height, and
            # falls through to the unrestricted selector if none exists -
            # never removing an option that would otherwise have downloaded.
            ydl_opts["format"] = (
                f"bestvideo[vcodec^=avc1]{height_filter}/{base_format}"
                if job.premiere_compat else base_format
            )
            if job.container in VIDEO_FORMATS:
                ydl_opts.setdefault("postprocessors", [])
                ydl_opts["postprocessors"].append({
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": job.container,
                })
        else:
            base_format = f"bestvideo{height_filter}+bestaudio/best{height_filter}"
            # Same reasoning as video_only above, plus [acodec^=mp4a] for
            # the audio half (YouTube's h264 streams are almost always
            # paired with AAC anyway, but this makes it explicit rather
            # than incidental) - each half falls through to the plain
            # bestvideo/bestaudio choice independently if no h264/aac
            # option exists at this height, same as premiere_compat off.
            ydl_opts["format"] = (
                f"bestvideo[vcodec^=avc1]{height_filter}+bestaudio[acodec^=mp4a]/{base_format}"
                if job.premiere_compat else base_format
            )
            if job.container in VIDEO_FORMATS:
                ydl_opts["merge_output_format"] = job.container

        if job.subtitle_lang and job.mode != "audio" and job.container in EMBEDDABLE_SUBTITLE_CONTAINERS:
            # Written as .srt and embedded (soft subs) directly into the video so
            # there's still exactly one output file — no orphaned subtitle file
            # left behind that nothing ever downloads or cleans up.
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = True
            ydl_opts["subtitleslangs"] = [job.subtitle_lang]
            ydl_opts.setdefault("postprocessors", [])
            ydl_opts["postprocessors"].append({
                "key": "FFmpegSubtitlesConvertor",
                "format": "srt",
            })
            ydl_opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

        def _clear_partial_output():
            # The failed anonymous attempt may have already written a
            # partial file before erroring out - clear it so the retry
            # starts clean instead of _find_main_file picking up stale
            # leftovers below.
            for name in os.listdir(out_dir):
                path = os.path.join(out_dir, name)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        info, used_cookies = _extract_with_cookie_fallback(
            ydl_opts, job.url, download=True,
            should_retry=lambda: job_id not in _cancel_requested,
            before_retry=_clear_partial_output,
        )

        title = (info or {}).get("title") or "video"
        filepath = _find_main_file(out_dir)
        filesize = os.path.getsize(filepath) if filepath and os.path.exists(filepath) else None

        if not filepath:
            raise RuntimeError("Не вдалося знайти завантажений файл")

        # Resolved *before* the job is marked "finished" (and committed
        # together with it below) so a poll landing right after "finished"
        # appears can never see it without auto_convert_id already set -
        # otherwise the frontend would briefly think the raw, incompatible
        # file is the final result and auto-download that instead.
        auto_convert_id = None
        if job.mode != "audio" and job.premiere_compat:
            # Deferred import: converter.py imports parse_timecode from this
            # module, so importing it back at module load time would be
            # circular - by the time this actually runs, both modules are
            # already fully loaded, so a call-time import resolves fine.
            from . import converter
            probed = converter.probe_input(filepath)
            if probed and not converter.is_premiere_compatible(probed["vcodec"], probed["acodec"]):
                job.filepath = filepath  # not committed yet - the conversion needs the real path to copy from
                try:
                    auto_convert_id = converter.submit_conversion_from_download(job)
                except Exception:
                    auto_convert_id = None

        _update(
            db, job,
            status="finished",
            progress=100.0,
            eta_seconds=None,
            title=title,
            filepath=filepath,
            filesize=filesize,
            auto_convert_id=auto_convert_id,
            used_cookies=used_cookies,
            finished_at=datetime.utcnow(),
        )
    except Exception as e:
        if job_id in _cancel_requested:
            _update(db, job, status="cancelled", eta_seconds=None, finished_at=datetime.utcnow())
        else:
            _update(db, job, status="error", eta_seconds=None, error_message=str(e)[:500], finished_at=datetime.utcnow())
        # Partial output from an aborted download shouldn't linger forever -
        # let the cleanup path treat it the same as any other dead job.
        out_dir = os.path.join(config.DOWNLOAD_DIR, job_id)
        if job_id in _cancel_requested and os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
    finally:
        _cancel_requested.discard(job_id)
        _gate.release()
        db.close()
