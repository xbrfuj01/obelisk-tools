import base64
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

VIEWPORTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}

FRAME_RATES = {30, 60}

DEVICE_MODES = {"desktop", "mobile"}

# Real per-CSS-pixel-width breakpoints mean a genuinely narrow viewport is
# what actually triggers a site's mobile layout - not just an is_mobile
# flag on a 1080-1920px-wide "desktop-shaped" viewport, which most sites'
# media queries would still treat as a large desktop/tablet. So "phone
# mode" uses Playwright's own verified "Pixel 7" device descriptor
# (viewport/UA/deviceScaleFactor/isMobile/hasTouch bundled together, exact
# values confirmed against playwright-core 1.47.0's own
# deviceDescriptorsSource.json) instead of trying to force a phone-like
# render into one of the aspect_ratio presets above - the two goals
# (accurate mobile rendering vs. an arbitrary chosen video frame shape)
# don't both fit at once, so mobile mode intentionally ignores
# aspect_ratio and just records at the phone's own natural shape.
MOBILE_DEVICE_NAME = "Pixel 7"
# Mirrors that same descriptor's viewport, only for reporting size to the
# preview frontend before a real browser/Playwright driver exists yet.
MOBILE_VIEWPORT = (412, 839)

# A storage/resource guard (this project's NVMe "apps" pool is small, see
# project-homelab-infra memory) - this is now the *output* video's length,
# not something capture paces itself against (see _scroll_and_capture and
# _encode below).
MIN_DURATION_SECONDS = 3
MAX_DURATION_SECONDS = 300

# Playwright's default headless Chromium UA can still read as automated to
# some checks - a plain modern desktop Chrome UA is a cheap, honest-effort
# improvement alongside playwright-stealth below. (Mobile mode gets its UA
# from the Pixel 7 device descriptor instead, see above.)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# A small curated list, not a full EasyList-format filter engine (parsing
# AdBlock Plus filter syntax is a much bigger, separately-maintained
# problem) - good enough for a personal recording tool. Registered on the
# page before goto so it also covers the initial page load's own ad
# requests, not just ones fired after.
AD_BLOCK_DOMAINS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google-analytics.com", "googletagmanager.com", "adnxs.com",
    "amazon-adsystem.com", "criteo.com", "criteo.net", "taboola.com",
    "outbrain.com", "pubmatic.com", "rubiconproject.com", "openx.net",
    "adform.net", "media.net", "revcontent.com", "mgid.com",
    "scorecardresearch.com", "quantserve.com", "moatads.com",
    "adsafeprotected.com", "bidswitch.net", "casalemedia.com",
    "smartadserver.com", "yieldmo.com", "sharethrough.com",
)

# Cosmetic fallback for ad markup the network block doesn't catch (already
# server-rendered, or served from a first-party path). Applied once after
# the initial settle wait, before any frame is ever captured.
AD_BLOCK_CSS = """
[id*="google_ads"], [id^="div-gpt-ad"], ins.adsbygoogle,
[class*="adsbygoogle"], iframe[src*="doubleclick"],
[class*="ad-container"], [class*="ad-slot"], [class*="advert"],
[id*="taboola"], [id*="outbrain"], [class*="sponsored-content"] {
  display: none !important;
}
"""

# The per-frame scroll step is computed per-recording (see
# _compute_scroll_step), not a fixed constant - sized directly from how
# tall the page is and how many frames the requested output actually needs
# (duration_seconds * framerate), so a short page doesn't get wastefully
# over-captured and a tall one doesn't take forever. These just bound that
# calculation on both ends: never so fine that a short page/long duration
# combo captures far more frames than the output could ever use, never so
# coarse (on a very tall page) that even the raw footage looks choppy
# before any speedup.
MIN_SCROLL_STEP_PX = 2
MAX_SCROLL_STEP_PX = 30

# Safety ceilings on the raw capture phase itself - a backstop for a page
# whose height keeps growing (infinite scroll) or that's simply too tall
# to finish even at MAX_SCROLL_STEP_PX, not the primary pacing mechanism
# (see above). MAX_CAPTURE_FRAMES in particular bounds temporary disk
# usage (this project's NVMe "apps" pool is small) - frames are deleted
# right after encoding either way.
MAX_CAPTURE_SECONDS = 900
MAX_CAPTURE_FRAMES = 6000

# How many consecutive ticks with zero net scroll movement before giving
# up on a stuck page (scroll-jacked layout, a sticky element fighting the
# scroll, etc.) instead of running until MAX_CAPTURE_SECONDS.
STALL_TICK_LIMIT = 8

RETENTION_SECONDS = 2 * 3600
CLEANUP_INTERVAL_SECONDS = 600

# A preview session holds a live headless Chromium instance idle while the
# user picks elements to remove - cap how many can be open at once (each is
# a real browser process) and close ones nobody's touched in a while so an
# abandoned picker tab doesn't leak a browser forever.
MAX_PREVIEW_SESSIONS = 3
PREVIEW_IDLE_SECONDS = 600

_executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_jobs_lock = threading.Lock()
_cancel_requested = set()

_previews = {}
_previews_lock = threading.Lock()


def create_job(
    url: str, aspect_ratio: str, device: str, duration_seconds: int, framerate: int,
    block_ads: bool = False, proxy_url: str = None,
    start_fraction: float = 0.0, end_fraction: float = 1.0,
) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": 0.0, "error": None}
    _executor.submit(
        _run_job, job_id, url, aspect_ratio, device, duration_seconds, framerate, block_ads, proxy_url,
        start_fraction, end_fraction,
    )
    return job_id


def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def request_cancel(job_id: str):
    _cancel_requested.add(job_id)


def job_file_path(job_id: str):
    job = get_job(job_id)
    if not job or job.get("status") != "finished":
        return None
    return os.path.join(DATA_DIR, job_id, "output.mp4")


def _set_status(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _parse_proxy(proxy_url: str):
    """Converts a yt-dlp-style proxy URL (the same string the admin's
    "Проксі для заблокованих сайтів" setting already stores, allowing
    socks5h/socks4a schemes and embedded user:pass@) into Playwright's
    proxy launch-option shape - which wants credentials as separate fields,
    not embedded in the server URL, and doesn't recognize the h/a
    DNS-resolution suffixes."""
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme in ("socks5", "socks5h"):
        scheme = "socks5"
    elif scheme in ("socks4", "socks4a"):
        scheme = "socks4"
    elif scheme not in ("http", "https"):
        scheme = "http"

    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _apply_ad_block(page):
    def _route_handler(route):
        if any(domain in route.request.url for domain in AD_BLOCK_DOMAINS):
            route.abort()
        else:
            route.continue_()

    page.route("**/*", _route_handler)


def _hide_ad_containers(page):
    page.add_style_tag(content=AD_BLOCK_CSS)


def _screenshot_b64(page) -> str:
    return base64.b64encode(page.screenshot()).decode("ascii")


def _prepare_page(p, browser, url, aspect_ratio, device, block_ads):
    if device == "mobile":
        page = browser.new_page(**p.devices[MOBILE_DEVICE_NAME])
    else:
        width, height = VIEWPORTS[aspect_ratio]
        page = browser.new_page(viewport={"width": width, "height": height}, user_agent=DESKTOP_USER_AGENT)
    # A native dialog (alert/confirm/prompt/beforeunload) blocks the page's
    # JS thread until dismissed - Playwright does not auto-dismiss these,
    # so without a handler any page that pops one mid-scroll freezes every
    # subsequent evaluate() call indefinitely (looks exactly like a
    # recording stuck at some fixed progress %, never erroring, never
    # finishing). Auto-dismissing keeps capture moving regardless.
    page.on("dialog", lambda dialog: dialog.dismiss())
    stealth_sync(page)
    if block_ads:
        _apply_ad_block(page)
    page.goto(url, wait_until="load", timeout=60000)
    page.wait_for_timeout(500)  # let late-loading content/lazy images settle
    if block_ads:
        _hide_ad_containers(page)
    return page


def _scroll_by(page, dy):
    """Scrolls by dy via an instant jump (never a page's own CSS
    scroll-behavior:smooth - see _scroll_and_capture for why) and waits
    for the browser to actually paint the new position before returning,
    so whatever's captured right after (a screenshot, or just reading the
    new position) reflects a crisp, settled frame instead of a
    mid-animation one. Shared by the real capture loop and the
    preview-session wheel-scroll endpoint."""
    return page.evaluate(
        """(dy) => new Promise((resolve) => {
            window.scrollBy({top: dy, left: 0, behavior: 'instant'});
            requestAnimationFrame(() => requestAnimationFrame(() => {
                resolve({y: window.scrollY, h: document.documentElement.scrollHeight});
            }));
        })""",
        dy,
    )


def _compute_scroll_step(total_scrollable_px, duration_seconds, output_fps):
    """Sizes the per-frame scroll step directly from the page's own
    scrollable distance and what the requested output actually needs
    (duration_seconds * output_fps frames) - not a fixed constant. The
    "ideal" step is exactly enough raw frames to cover the whole page once
    each, spread evenly across the target frame count: no waste
    over-capturing a short page, no needing to when a page is short
    relative to a long requested duration. Clamped on both ends: a floor
    so a short page / long duration combo doesn't chase a near-zero step
    forever, a ceiling so a very tall page still gets reasonably fine-
    grained raw footage instead of looking choppy even before any
    speedup - accepting a longer real capture time in that case rather
    than a coarser one, since _encode's select/setpts speedup can still
    smooth out an oversampled sequence but can't fix an undersampled one."""
    target_output_frames = max(1, round(duration_seconds * output_fps))
    if total_scrollable_px <= 0:
        return MIN_SCROLL_STEP_PX
    ideal_step_px = total_scrollable_px / target_output_frames
    return max(MIN_SCROLL_STEP_PX, min(MAX_SCROLL_STEP_PX, round(ideal_step_px)))


def _scroll_and_capture(
    job_id, page, frames_dir, duration_seconds, output_fps,
    start_fraction=0.0, end_fraction=1.0,
):
    """Captures one frame per _compute_scroll_step() pixels of real scroll
    movement, from start_fraction to end_fraction of the page's scrollable
    distance (defaults 0.0/1.0 - the whole page) - deliberately not paced
    to a time deadline itself (see _compute_scroll_step), so the raw
    footage this produces stays fine-grained regardless of how long it
    takes in real wall-clock time. _encode (called separately, after this
    returns) is what fits the result into the exact requested output
    duration/framerate, by speeding up this footage rather than by pacing
    the capture itself to a deadline.

    start_fraction/end_fraction are resolved against a fresh scrollHeight
    reading taken right here (the same one already needed to size the
    capture step), not a stale measurement from whenever a preview session
    was first opened - the frontend only ever sends fractions, computed
    the same way it already tracks "how far scrolled" from wheel-scroll
    responses (y / (h - viewportHeight)), so a marker placed at some
    fraction here means the same page position regardless of when it was
    set."""
    height = page.viewport_size["height"]
    deadline = time.monotonic() + MAX_CAPTURE_SECONDS

    state = page.evaluate(
        "() => ({y: window.scrollY, h: document.documentElement.scrollHeight})"
    )
    scroll_height = state["h"]
    total_page_scrollable = max(0, scroll_height - height)

    start_fraction = max(0.0, min(1.0, start_fraction))
    end_fraction = max(0.0, min(1.0, end_fraction))
    start_px = round(start_fraction * total_page_scrollable)
    # stop_scroll_y is a scrollY *target* (same units/range as start_px),
    # not a page-content boundary - the frontend computes both start/end
    # fractions identically (an absolute page position divided by
    # total_page_scrollable), so both sides of this need the same
    # convention or the end marker silently lands somewhere else entirely
    # (previously off by exactly one viewport height, from mixing the two
    # conventions between client and server).
    fixed_end = end_fraction < 1.0
    stop_scroll_y = round(end_fraction * total_page_scrollable) if fixed_end else total_page_scrollable

    scroll_y = state["y"]
    if start_px != scroll_y:
        state = _scroll_by(page, start_px - scroll_y)
        scroll_y = state["y"]

    frame_index = 0
    stall_ticks = 0
    range_total = max(0, stop_scroll_y - start_px)
    step_px = _compute_scroll_step(range_total, duration_seconds, output_fps)

    while True:
        if job_id in _cancel_requested:
            break
        if time.monotonic() >= deadline or frame_index >= MAX_CAPTURE_FRAMES:
            break
        # Some pages pin/reset scrollY themselves (scroll-jacked "sections"
        # layouts, a sticky element fighting our scroll, etc.) - scrollBy
        # then has no real effect, and without this the loop would just
        # keep capturing identical frames until MAX_CAPTURE_SECONDS. Give
        # up early instead once several consecutive ticks made no progress,
        # and encode whatever was captured so far rather than stalling.
        if stall_ticks >= STALL_TICK_LIMIT:
            break

        # Lossless PNG - JPEG's compression, stacked with the H.264 encode
        # afterward, was producing double-compression ringing/ghosting
        # specifically on sharp text edges.
        page.screenshot(path=os.path.join(frames_dir, f"frame_{frame_index:06d}.png"))
        frame_index += 1

        progressed = max(0, scroll_y - start_px)
        progress = min(99.0, progressed / range_total * 100) if range_total > 0 else 100.0
        _set_status(job_id, progress=round(progress, 1))

        remaining_px = max(0, stop_scroll_y - scroll_y)
        if remaining_px <= 0:
            break

        step = min(step_px, remaining_px)
        state = _scroll_by(page, step)
        new_scroll_y = state["y"]
        stall_ticks = stall_ticks + 1 if new_scroll_y <= scroll_y else 0
        scroll_y = new_scroll_y
        if not fixed_end:
            # only "record to the actual bottom" mode adapts to lazy-
            # loaded growth - an explicit trim is a deliberately fixed
            # endpoint, not re-extended by content that loads in later
            scroll_height = max(scroll_height, state["h"])
            total_page_scrollable = max(0, scroll_height - height)
            stop_scroll_y = total_page_scrollable

    return frame_index


def _encode(frames_dir, out_path, frame_count, duration_seconds, output_fps):
    # The raw footage has far more frames, each a few pixels apart, than
    # the requested output needs - "speed up" to the target by *selecting*
    # every Nth real frame (never blending/interpolating, which is exactly
    # what looked like smeared "swimming" ghosting in earlier testing) and
    # re-timing the kept frames to an even output_fps via setpts. If the
    # raw footage doesn't even have enough frames for the target duration
    # at this framerate (a short page + a long requested duration),
    # skip_n floors at 1 (keep every frame) and the output just ends up
    # shorter than requested rather than needing to duplicate anything.
    total_output_frames = max(1, round(duration_seconds * output_fps))
    skip_n = max(1, round(frame_count / total_output_frames))
    video_filter = f"select='not(mod(n\\,{skip_n}))',setpts=N/{output_fps}/TB"
    cmd = [
        "ffmpeg", "-y",
        "-i", os.path.join(frames_dir, "frame_%06d.png"),
        "-vf", video_filter,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {result.stderr[-2000:]}")


def _finalize_recording(job_id, job_dir, frames_dir, frame_count, duration_seconds, output_fps):
    if job_id in _cancel_requested:
        _set_status(job_id, status="cancelled")
        return
    if frame_count == 0:
        _set_status(job_id, status="error", error="Не вдалося захопити жодного кадру")
        return

    _set_status(job_id, status="encoding")
    out_path = os.path.join(job_dir, "output.mp4")
    _encode(frames_dir, out_path, frame_count, duration_seconds, output_fps)
    shutil.rmtree(frames_dir, ignore_errors=True)
    _set_status(job_id, status="finished", progress=100.0)


def _run_job(
    job_id, url, aspect_ratio, device, duration_seconds, framerate, block_ads, proxy_url,
    start_fraction=0.0, end_fraction=1.0,
):
    job_dir = os.path.join(DATA_DIR, job_id)
    frames_dir = os.path.join(job_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    try:
        _set_status(job_id, status="recording")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=_parse_proxy(proxy_url))
            try:
                page = _prepare_page(p, browser, url, aspect_ratio, device, block_ads)
                frame_count = _scroll_and_capture(
                    job_id, page, frames_dir, duration_seconds, framerate, start_fraction, end_fraction
                )
            finally:
                browser.close()
        _finalize_recording(job_id, job_dir, frames_dir, frame_count, duration_seconds, framerate)
    except Exception as exc:
        _set_status(job_id, status="error", error=str(exc))
    finally:
        _cancel_requested.discard(job_id)


def _finish_job_on_page(job_id, page, duration_seconds, framerate, start_fraction=0.0, end_fraction=1.0):
    """Same tail as _run_job, but reuses an already-live page (from a
    preview session) instead of launching a fresh browser - whatever
    ad-block routes/hidden elements are already on the page just carry
    over, nothing needs to be replayed."""
    job_dir = os.path.join(DATA_DIR, job_id)
    frames_dir = os.path.join(job_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    try:
        _set_status(job_id, status="recording")
        frame_count = _scroll_and_capture(
            job_id, page, frames_dir, duration_seconds, framerate, start_fraction, end_fraction
        )
        _finalize_recording(job_id, job_dir, frames_dir, frame_count, duration_seconds, framerate)
    except Exception as exc:
        _set_status(job_id, status="error", error=str(exc))
    finally:
        _cancel_requested.discard(job_id)


class PreviewSession:
    """One dedicated worker thread per session, owning one live Playwright
    page. Required because Playwright's sync API is not thread-safe across
    threads - a page/browser must only ever be touched from the thread that
    created it. Every action (a click, an undo, or the final recording) is
    submitted as a callable through an internal queue and run on that
    thread; callers block on a Future to get the result back."""

    def __init__(
        self, session_id: str, url: str, aspect_ratio: str, device: str,
        block_ads: bool, proxy_url: str = None,
    ):
        self.id = session_id
        self.width, self.height = MOBILE_VIEWPORT if device == "mobile" else VIEWPORTS[aspect_ratio]
        self.last_active = time.monotonic()
        self._queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._removed_stack = []
        self._removed_counter = 0

        ready = Future()
        self._thread = threading.Thread(
            target=self._run, args=(url, aspect_ratio, device, block_ads, proxy_url, ready), daemon=True
        )
        self._thread.start()
        initial = ready.result(timeout=65)
        self.screenshot_b64 = initial["screenshot"]
        self.y = initial["y"]
        self.h = initial["h"]

    def _run(self, url, aspect_ratio, device, block_ads, proxy_url, ready):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, proxy=_parse_proxy(proxy_url))
                try:
                    page = _prepare_page(p, browser, url, aspect_ratio, device, block_ads)
                    state = page.evaluate(
                        "() => ({y: window.scrollY, h: document.documentElement.scrollHeight})"
                    )
                    ready.set_result(
                        {"screenshot": _screenshot_b64(page), "y": state["y"], "h": state["h"]}
                    )
                    while True:
                        item = self._queue.get()
                        if item is None:
                            break
                        func, fut, terminal = item
                        try:
                            fut.set_result(func(page))
                        except Exception as exc:
                            fut.set_exception(exc)
                        if terminal:
                            break
                finally:
                    browser.close()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)

    def call(self, func, timeout=30):
        self.last_active = time.monotonic()
        fut = Future()
        self._queue.put((func, fut, False))
        return fut.result(timeout=timeout)

    def call_terminal(self, func):
        """Submits a long-running action (the actual recording) without
        waiting for it - the thread closes its own browser and exits once
        func returns, same lifecycle as a normal direct-record job."""
        self.last_active = time.monotonic()
        fut = Future()
        self._queue.put((func, fut, True))

    def close(self):
        self._queue.put(None)

    def next_removed_index(self):
        with self._state_lock:
            self._removed_counter += 1
            return self._removed_counter

    def push_removed(self, idx):
        with self._state_lock:
            self._removed_stack.append(idx)

    def pop_removed(self):
        with self._state_lock:
            return self._removed_stack.pop() if self._removed_stack else None


def _get_preview(session_id: str) -> PreviewSession:
    with _previews_lock:
        session = _previews.get(session_id)
    if not session:
        raise KeyError(session_id)
    return session


def create_preview(url: str, aspect_ratio: str, device: str, block_ads: bool, proxy_url: str = None):
    with _previews_lock:
        if len(_previews) >= MAX_PREVIEW_SESSIONS:
            raise RuntimeError("Забагато активних попередніх переглядів, спробуйте пізніше")

    session_id = uuid.uuid4().hex
    session = PreviewSession(session_id, url, aspect_ratio, device, block_ads, proxy_url)
    with _previews_lock:
        _previews[session_id] = session
    return session_id, session.screenshot_b64, session.width, session.height, session.y, session.h


def scroll_preview(session_id: str, delta_y: float) -> dict:
    session = _get_preview(session_id)

    def action(page):
        state = _scroll_by(page, delta_y)
        return {"screenshot": _screenshot_b64(page), "y": state["y"], "h": state["h"]}

    return session.call(action)


def remove_at_point(session_id: str, x: float, y: float) -> str:
    session = _get_preview(session_id)
    idx = session.next_removed_index()

    def action(page):
        removed = page.evaluate(
            """([x, y, idx]) => {
                const el = document.elementFromPoint(x, y);
                if (!el || el === document.body || el === document.documentElement) return false;
                el.setAttribute('data-obelisk-removed', String(idx));
                el.style.setProperty('display', 'none', 'important');
                return true;
            }""",
            [x, y, idx],
        )
        if removed:
            session.push_removed(idx)
        return _screenshot_b64(page)

    return session.call(action)


def remove_header(session_id: str) -> str:
    """Removes the site's own top bar (logo/nav) without requiring the
    user to click it directly - probes the same point every page's header
    naturally sits at (top-center), then walks up from whatever element
    is there to the nearest ancestor that actually looks like a header bar
    (a <header>, or anything fixed/sticky spanning at least half the
    viewport width), so a click landing on e.g. just the logo image still
    hides the whole bar around it instead of one small child element."""
    session = _get_preview(session_id)
    idx = session.next_removed_index()

    def action(page):
        removed = page.evaluate(
            """(idx) => {
                const start = document.elementFromPoint(window.innerWidth / 2, 5);
                if (!start || start === document.body || start === document.documentElement) return false;
                let el = start;
                let candidate = null;
                while (el && el !== document.body) {
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const barLike = (style.position === 'fixed' || style.position === 'sticky' || el.tagName === 'HEADER')
                        && rect.width >= window.innerWidth * 0.5;
                    if (barLike) { candidate = el; break; }
                    el = el.parentElement;
                }
                const target = candidate || start;
                target.setAttribute('data-obelisk-removed', String(idx));
                target.style.setProperty('display', 'none', 'important');
                return true;
            }""",
            idx,
        )
        if removed:
            session.push_removed(idx)
        return _screenshot_b64(page)

    return session.call(action)


def undo_last(session_id: str) -> str:
    session = _get_preview(session_id)
    idx = session.pop_removed()

    def action(page):
        if idx is not None:
            page.evaluate(
                """(idx) => {
                    const el = document.querySelector(`[data-obelisk-removed="${idx}"]`);
                    if (el) {
                        el.style.removeProperty('display');
                        el.removeAttribute('data-obelisk-removed');
                    }
                }""",
                idx,
            )
        return _screenshot_b64(page)

    return session.call(action)


def start_recording_from_preview(
    session_id: str, duration_seconds: int, framerate: int,
    start_fraction: float = 0.0, end_fraction: float = 1.0,
) -> str:
    with _previews_lock:
        session = _previews.pop(session_id, None)
    if not session:
        raise KeyError(session_id)

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": 0.0, "error": None}

    session.call_terminal(
        lambda page: _finish_job_on_page(job_id, page, duration_seconds, framerate, start_fraction, end_fraction)
    )
    return job_id


def close_preview(session_id: str):
    with _previews_lock:
        session = _previews.pop(session_id, None)
    if session:
        session.close()


def _cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)

        now = time.time()
        try:
            names = os.listdir(DATA_DIR)
        except OSError:
            names = []
        for name in names:
            path = os.path.join(DATA_DIR, name)
            if not os.path.isdir(path):
                continue
            try:
                if now - os.path.getmtime(path) > RETENTION_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
                    with _jobs_lock:
                        _jobs.pop(name, None)
            except OSError:
                continue

        now_monotonic = time.monotonic()
        with _previews_lock:
            stale_ids = [
                sid for sid, session in _previews.items()
                if now_monotonic - session.last_active > PREVIEW_IDLE_SECONDS
            ]
            stale_sessions = [_previews.pop(sid) for sid in stale_ids]
        for session in stale_sessions:
            session.close()


threading.Thread(target=_cleanup_loop, daemon=True).start()
