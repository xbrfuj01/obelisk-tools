import os
import secrets
import uuid
from datetime import datetime

from fastapi import (
    FastAPI, File, Request, Response, Form, Depends, HTTPException, UploadFile,
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse,
)
from starlette.staticfiles import StaticFiles as _StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from . import config
from .database import init_db, SessionLocal
from .models import Notification, User
from . import auth
from . import timeutil
from . import sysinfo
from . import modules
from . import proxy as proxy_helpers

BASE_DIR = os.path.dirname(__file__)


class StaticFiles(_StaticFiles):
    """Forces browsers to revalidate static assets (JS/CSS) on every load
    instead of serving a stale cached copy after a deploy. The image gets
    fully rebuilt on every push, so file mtimes/ETags always change - the
    browser will get a fast 304 when nothing changed and the real new file
    the moment something did, rather than silently running old JS until
    someone thinks to hard-refresh."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# The session cookie needs a secret key and max_age before the app object
# even exists, so the DB is bootstrapped here rather than in a startup
# event — that also means the app never needs an env var for any of this.
init_db()
_bootstrap_db = SessionLocal()
try:
    auth.ensure_secret_key(_bootstrap_db)
    _secret_key = auth.get_secret_key(_bootstrap_db)
    _session_max_age_days = auth.get_session_max_age_days(_bootstrap_db)
finally:
    _bootstrap_db.close()

app = FastAPI(title="Obelisk")
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
    session_cookie="vd_session",
    max_age=_session_max_age_days * 86400,
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["format_dt"] = timeutil.format_local
templates.env.globals["format_bytes"] = sysinfo.format_bytes


def _is_admin_request(request: Request) -> bool:
    """Jinja global so base.html can decide whether to show the admin
    entry-point icon without every single page route needing to pass it
    through their template context explicitly."""
    db = SessionLocal()
    try:
        return auth.is_admin_session(request, db)
    finally:
        db.close()


templates.env.globals["is_admin_request"] = _is_admin_request
templates.env.globals["is_user_online"] = auth.is_user_online


# Everything except the setup page itself and the assets that page needs.
# Deliberately a prefix list rather than a route-by-route dependency: a new
# route added later is closed by default instead of silently being the one
# hole left in a site that has no owner yet.
SETUP_ALLOWED_PREFIXES = ("/setup", "/static/")


@app.middleware("http")
async def setup_gate(request: Request, call_next):
    """Until the first admin exists, the site answers nothing but /setup.

    Without this a fresh install is not merely unconfigured but wide open:
    the login gate only switches on once a user row exists (see
    auth.is_site_gate_enabled), so before setup every tool would be usable
    by anyone who found the address."""
    if not auth.setup_completed_cached():
        db = SessionLocal()
        try:
            pending = not auth.is_setup_completed(db)
        finally:
            db.close()
        path = request.url.path
        if pending and not path.startswith(SETUP_ALLOWED_PREFIXES):
            # base.html's polling scripts run on the setup page too - they
            # get a plain JSON error rather than a 303 into an HTML page,
            # which they'd fail to parse and log as a console error.
            if path.startswith("/api/"):
                return JSONResponse({"error": "Сайт ще не налаштовано"}, status_code=503)
            return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


@app.exception_handler(auth.NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse("/site-login", status_code=303)


@app.exception_handler(auth.SiteNotAuthenticated)
async def site_not_authenticated_handler(request: Request, exc: auth.SiteNotAuthenticated):
    return RedirectResponse("/site-login", status_code=303)


class ModuleDisabled(Exception):
    pass


@app.exception_handler(ModuleDisabled)
async def module_disabled_handler(request: Request, exc: ModuleDisabled):
    # Every module's own frontend JS reads response bodies as {"error": ...}
    # (see proxy.py's _normalize_error_payload, which exists for exactly
    # this reason) - a plain HTTPException(detail=...) would serialize as
    # {"detail": ...} instead and silently fail to show anything.
    return JSONResponse({"error": "Модуль недоступний"}, status_code=503)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


CLIENT_ID_COOKIE = "client_id"
CLIENT_ID_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days


def _client_id_and_flag(request: Request):
    """Read-only half of the client_id cookie dance - doesn't touch any
    Response, since most callers here return a proxied Response object
    directly (not a plain dict), and FastAPI only merges an injected
    `response: Response` parameter's cookies into the final response when
    the route returns non-Response data for it to wrap. Returning our own
    Response bypasses that merge silently, so the cookie has to be set on
    the actual object being returned instead - see _with_client_id_cookie."""
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    is_new = not client_id
    if is_new:
        client_id = uuid.uuid4().hex
    return client_id, is_new


def _with_client_id_cookie(resp, client_id: str, is_new: bool):
    if is_new:
        resp.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


def require_site_access_page(request: Request, db: Session = Depends(get_db)):
    auth.require_site_access(request, db)
    auth.record_activity(db, request.session.get("site_username"))


def require_site_access_api(request: Request, db: Session = Depends(get_db)):
    if auth.is_site_gate_enabled(db) and not request.session.get("site_access"):
        raise HTTPException(status_code=401, detail="Потрібен пароль сайту")
    auth.record_activity(db, request.session.get("site_username"))


def require_admin_dep(request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request, db)
    auth.record_activity(db, request.session.get("site_username"))


def _announce_setup_if_pending():
    """Prints the one-time setup token to the container log on every start
    while there's still no admin. The log is the one channel that already
    requires server-side access to read, which is exactly the property the
    token needs - and reprinting it every start means a forgotten or
    scrolled-away token is one restart away, with the old one dead."""
    db = SessionLocal()
    try:
        auth.ensure_setup_state(db)
        if auth.is_setup_completed(db):
            return
        token = auth.issue_setup_token()
    finally:
        db.close()
    line = "=" * 68
    print(
        f"\n{line}\n"
        "Obelisk: адміністратора ще не створено.\n"
        "Відкрийте сайт — він сам переадресує на /setup — і введіть цей код:\n\n"
        f"    {token}\n\n"
        "Код дійсний до перезапуску контейнера і ніде не зберігається.\n"
        f"{line}\n",
        flush=True,
    )


@app.on_event("startup")
def on_startup():
    _announce_setup_if_pending()
    _bootstrap = SessionLocal()
    try:
        modules.load_enabled_state(_bootstrap)
    finally:
        _bootstrap.close()
    modules.start_health_check_thread()
    modules.push_downloader_converter_config()


# ---------------- Public ----------------

def _module_ok(request: Request, db: Session, name: str) -> bool:
    """The one check page/API routes for `name` should gate on: reachable
    (health) AND not turned off by an admin for this request's viewer."""
    return modules.is_module_available(name) and modules.is_enabled_for(request, db, name)


def _module_unavailable(request: Request):
    return templates.TemplateResponse(
        "module_unavailable.html", {"request": request}, status_code=503
    )


@app.get("/", response_class=HTMLResponse)
def hub(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    available = {name: _module_ok(request, db, name) for name in config.MODULE_URLS}
    return templates.TemplateResponse("hub.html", {"request": request, "available": available})


@app.get("/downloader", response_class=HTMLResponse)
async def downloader_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    if not _module_ok(request, db, "downloader_converter"):
        return _module_unavailable(request)
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    is_new_client = not client_id
    if is_new_client:
        client_id = uuid.uuid4().hex

    data = await proxy_helpers.fetch_json(
        config.DOWNLOADER_CONVERTER_URL, "/jobs/download",
        params={"client_id": client_id, "page": 1}, default={},
    )
    resp = templates.TemplateResponse(
        "downloader.html", {"request": request, "recent": data.get("items", [])}
    )
    if is_new_client:
        resp.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


@app.get("/converter", response_class=HTMLResponse)
async def converter_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    if not _module_ok(request, db, "downloader_converter"):
        return _module_unavailable(request)
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    is_new_client = not client_id
    if is_new_client:
        client_id = uuid.uuid4().hex

    data = await proxy_helpers.fetch_json(
        config.DOWNLOADER_CONVERTER_URL, "/jobs/convert",
        params={"client_id": client_id, "page": 1}, default={},
    )
    resp = templates.TemplateResponse(
        "converter.html",
        {"request": request, "recent": data.get("items", []), "max_upload_mb": auth.get_max_upload_mb(db)},
    )
    if is_new_client:
        resp.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


@app.get("/metadata", response_class=HTMLResponse)
def metadata_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    if not _module_ok(request, db, "metadata"):
        return _module_unavailable(request)
    return templates.TemplateResponse(
        "metadata.html", {"request": request, "max_upload_mb": auth.get_max_upload_mb(db)}
    )


@app.get("/scroll-recorder", response_class=HTMLResponse)
def scroll_recorder_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    if not _module_ok(request, db, "scroll_recorder"):
        return _module_unavailable(request)
    return templates.TemplateResponse("scroll_recorder.html", {"request": request})


# Scroll Recorder itself runs in its own container (docker-compose.yml),
# unreachable from outside the compose network - these routes are a thin
# proxy so the browser only ever talks to this app's own origin, inheriting
# the site's normal session auth/rate-limiting instead of needing its own.
# See proxy.py for the generalized helpers this and future modules share.
async def _proxy_scroll_recorder(method: str, path: str, json_body=None, timeout=10):
    return await proxy_helpers.proxy_json(
        config.SCROLL_RECORDER_URL, method, path, json_body=json_body, timeout=timeout,
        unavailable_message="Модуль запису недоступний",
    )


def require_module_enabled(module_name: str):
    """Dependency factory blocking a module's own API routes for anyone but
    an admin while it's turned off (modules.is_enabled_for)."""
    def _dep(request: Request, db: Session = Depends(get_db)):
        if not modules.is_enabled_for(request, db, module_name):
            raise ModuleDisabled()
    return _dep


@app.post("/api/scroll-recorder/jobs")
async def create_scroll_recorder_job(
    request: Request, db: Session = Depends(get_db),
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"sr:{ip}"):
        return JSONResponse(
            {"error": "Забагато записів поспіль. Спробуйте пізніше."}, status_code=429
        )
    body = await request.json()
    # The actual proxy URL (with embedded credentials) never reaches the
    # browser - the frontend only sends whether it wants one, and this is
    # the only place with DB access to resolve it into a real value.
    if body.pop("use_proxy", False):
        body["proxy_url"] = auth.get_proxy_url(db) or None
    return await _proxy_scroll_recorder("POST", "/jobs", json_body=body, timeout=30)


@app.get("/api/scroll-recorder/jobs/{job_id}")
async def scroll_recorder_status(
    job_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    return await _proxy_scroll_recorder("GET", f"/jobs/{job_id}")


@app.delete("/api/scroll-recorder/jobs/{job_id}")
async def cancel_scroll_recorder_job(
    job_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    return await _proxy_scroll_recorder("DELETE", f"/jobs/{job_id}")


# Preview sessions open a real browser on the sidecar and hold it open
# while the user clicks elements to remove - /preview itself can take a
# while (a real page load), the rest are quick screenshot round-trips.
@app.post("/api/scroll-recorder/preview")
async def create_scroll_recorder_preview(
    request: Request, db: Session = Depends(get_db),
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"sr:{ip}"):
        return JSONResponse(
            {"error": "Забагато записів поспіль. Спробуйте пізніше."}, status_code=429
        )
    body = await request.json()
    if body.pop("use_proxy", False):
        body["proxy_url"] = auth.get_proxy_url(db) or None
    return await _proxy_scroll_recorder("POST", "/preview", json_body=body, timeout=65)


@app.post("/api/scroll-recorder/preview/{session_id}/remove")
async def scroll_recorder_preview_remove(
    session_id: str, request: Request,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    body = await request.json()
    return await _proxy_scroll_recorder(
        "POST", f"/preview/{session_id}/remove", json_body=body, timeout=30
    )


@app.post("/api/scroll-recorder/preview/{session_id}/undo")
async def scroll_recorder_preview_undo(
    session_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    return await _proxy_scroll_recorder("POST", f"/preview/{session_id}/undo", timeout=30)


@app.post("/api/scroll-recorder/preview/{session_id}/remove-header")
async def scroll_recorder_preview_remove_header(
    session_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    return await _proxy_scroll_recorder("POST", f"/preview/{session_id}/remove-header", timeout=30)


@app.post("/api/scroll-recorder/preview/{session_id}/scroll")
async def scroll_recorder_preview_scroll(
    session_id: str, request: Request,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    body = await request.json()
    return await _proxy_scroll_recorder(
        "POST", f"/preview/{session_id}/scroll", json_body=body, timeout=30
    )


@app.post("/api/scroll-recorder/preview/{session_id}/record")
async def scroll_recorder_preview_record(
    session_id: str, request: Request,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    body = await request.json()
    return await _proxy_scroll_recorder(
        "POST", f"/preview/{session_id}/record", json_body=body, timeout=10
    )


@app.delete("/api/scroll-recorder/preview/{session_id}")
async def cancel_scroll_recorder_preview(
    session_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    return await _proxy_scroll_recorder("DELETE", f"/preview/{session_id}", timeout=10)


@app.get("/api/scroll-recorder/jobs/{job_id}/file")
async def scroll_recorder_file(
    job_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("scroll_recorder")),
):
    return await proxy_helpers.proxy_file_stream(
        config.SCROLL_RECORDER_URL, f"/jobs/{job_id}/file",
        unavailable_message="Модуль запису недоступний",
    )


# ---------------- Downloader + Converter ----------------
# Both run together in their own module container (docker-compose.yml),
# with their own DB (Download/Conversion rows no longer live in core's) -
# core does auth/rate-limiting/client-id here, then proxies through to it,
# the same pattern as Scroll Recorder and the metadata editor. URL/settings
# validation (is_url_allowed, proxy resolution, concurrency limits) all
# happens module-side now, since that's where the relevant settings
# (pushed via modules.push_downloader_converter_config) actually live.

@app.post("/api/download")
async def create_download(
    request: Request,
    url: str = Form(...),
    mode: str = Form("video"),
    quality: str = Form("best"),
    container: str = Form("mp4"),
    subtitle_lang: str = Form(""),
    premiere_compat: bool = Form(False),
    clip_start: str = Form(""),
    clip_end: str = Form(""),
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    client_id, is_new = _client_id_and_flag(request)
    ip = request.client.host if request.client else "unknown"

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "Некоректне посилання"}, status_code=400)
    if not auth.check_download_rate_limit(f"dl:{ip}"):
        return JSONResponse(
            {"error": "Забагато завантажень поспіль. Спробуйте пізніше."}, status_code=429
        )

    body = {
        "url": url, "mode": mode, "quality": quality, "container": container,
        "subtitle_lang": subtitle_lang, "premiere_compat": premiere_compat,
        "clip_start": clip_start, "clip_end": clip_end,
        "client_id": client_id,
        "client_ip": request.client.host if request.client else None,
        "username": request.session.get("site_username"),
    }
    resp = await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "POST", "/jobs/download", json_body=body, timeout=30,
        unavailable_message="Модуль завантажень недоступний",
    )
    return _with_client_id_cookie(resp, client_id, is_new)


@app.get("/api/status/{job_id}")
async def job_status(
    job_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    return await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "GET", f"/jobs/download/{job_id}",
        unavailable_message="Модуль завантажень недоступний",
    )


@app.post("/api/cancel/{job_id}")
async def cancel_download(
    job_id: str, request: Request,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    client_id, is_new = _client_id_and_flag(request)
    resp = await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "POST", f"/jobs/download/{job_id}/cancel",
        json_body={"client_id": client_id},
        unavailable_message="Модуль завантажень недоступний",
    )
    return _with_client_id_cookie(resp, client_id, is_new)


@app.get("/api/formats")
async def get_formats(
    request: Request, url: str,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    ip = request.client.host if request.client else "unknown"
    # A read-only probe, not a real download/conversion - gets more
    # headroom than auth.DOWNLOAD_RATE_LIMIT under the same window, since
    # trying a few links or re-checking one after changing quality/mode
    # shouldn't compete with that much lower-traffic limit.
    if not auth.check_download_rate_limit(f"fmt:{ip}", limit=60):
        return JSONResponse(
            {"error": "Забагато запитів поспіль. Спробуйте пізніше."}, status_code=429
        )
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "Некоректне посилання"}, status_code=400)
    return await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "GET", "/jobs/formats", params={"url": url}, timeout=30,
        unavailable_message="Модуль завантажень недоступний",
    )


@app.get("/api/recent")
async def recent_jobs(
    request: Request, page: int = 1,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    client_id, is_new = _client_id_and_flag(request)
    resp = await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "GET", "/jobs/download",
        params={"client_id": client_id, "page": page},
        unavailable_message="Модуль завантажень недоступний",
    )
    return _with_client_id_cookie(resp, client_id, is_new)


@app.get("/api/processes")
async def processes(request: Request, response: Response, _=Depends(require_site_access_api)):
    """Combined active + recent downloads/conversions for the top-right
    processes tray - kept in one feed (rather than two) so it reads as a
    single timeline regardless of which tool the job came from. Falls back
    to an empty tray (rather than an error) if the module is unreachable.
    fetch_json returns plain data (not a Response), so - unlike most other
    routes here - FastAPI does merge this injected `response`'s cookie into
    the final response automatically."""
    client_id, is_new = _client_id_and_flag(request)
    if is_new:
        response.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return await proxy_helpers.fetch_json(
        config.DOWNLOADER_CONVERTER_URL, "/jobs/processes", params={"client_id": client_id}, default=[],
    )


@app.get("/api/file/{job_id}")
async def download_file(
    job_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    return await proxy_helpers.proxy_file_stream(
        config.DOWNLOADER_CONVERTER_URL, f"/jobs/download/{job_id}/file",
        unavailable_message="Модуль завантажень недоступний",
    )


@app.post("/api/convert")
async def create_conversion(
    request: Request,
    file: UploadFile = File(...),
    quality: str = Form("high"),
    audio_option: str = Form("original"),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"cv:{ip}"):
        return JSONResponse(
            {"error": "Забагато конвертацій поспіль. Спробуйте пізніше."}, status_code=429
        )
    client_id, is_new = _client_id_and_flag(request)
    max_upload_mb = auth.get_max_upload_mb(db)
    resp = await proxy_helpers.proxy_upload(
        config.DOWNLOADER_CONVERTER_URL, "/jobs/convert", file, max_upload_mb * 1024 * 1024,
        os.path.join(config.DATA_DIR, "tmp_uploads"),
        extra_fields={
            "quality": quality, "audio_option": audio_option, "client_id": client_id,
            "client_ip": ip, "username": request.session.get("site_username") or "",
        },
        too_large_message=f"Файл перевищує ліміт {max_upload_mb} МБ",
        unavailable_message="Модуль конвертера недоступний",
    )
    return _with_client_id_cookie(resp, client_id, is_new)


@app.post("/api/convert/from-download/{download_id}")
async def create_conversion_from_download(
    download_id: str,
    request: Request,
    quality: str = Form("high"),
    audio_option: str = Form("original"),
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"cv:{ip}"):
        return JSONResponse(
            {"error": "Забагато конвертацій поспіль. Спробуйте пізніше."}, status_code=429
        )
    client_id, is_new = _client_id_and_flag(request)
    resp = await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "POST", f"/jobs/convert/from-download/{download_id}",
        json_body={
            "quality": quality, "audio_option": audio_option, "client_id": client_id,
            "client_ip": ip, "username": request.session.get("site_username"),
        },
        unavailable_message="Модуль конвертера недоступний",
    )
    return _with_client_id_cookie(resp, client_id, is_new)


@app.get("/api/convert/status/{job_id}")
async def conversion_status(
    job_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    return await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "GET", f"/jobs/convert/{job_id}",
        unavailable_message="Модуль конвертера недоступний",
    )


@app.post("/api/convert/cancel/{job_id}")
async def cancel_conversion(
    job_id: str, request: Request,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    client_id, is_new = _client_id_and_flag(request)
    resp = await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "POST", f"/jobs/convert/{job_id}/cancel",
        json_body={"client_id": client_id},
        unavailable_message="Модуль конвертера недоступний",
    )
    return _with_client_id_cookie(resp, client_id, is_new)


@app.get("/api/convert/recent")
async def recent_conversions(
    request: Request, page: int = 1,
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    client_id, is_new = _client_id_and_flag(request)
    resp = await proxy_helpers.proxy_json(
        config.DOWNLOADER_CONVERTER_URL, "GET", "/jobs/convert",
        params={"client_id": client_id, "page": page},
        unavailable_message="Модуль конвертера недоступний",
    )
    return _with_client_id_cookie(resp, client_id, is_new)


@app.get("/api/convert/file/{job_id}")
async def conversion_file(
    job_id: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("downloader_converter")),
):
    return await proxy_helpers.proxy_file_stream(
        config.DOWNLOADER_CONVERTER_URL, f"/jobs/convert/{job_id}/file",
        unavailable_message="Модуль конвертера недоступний",
    )


# ---------------- Metadata editor ----------------
# Runs in its own module container (docker-compose.yml) - stateless on this
# side too (no DB row), core only does auth/rate-limiting/upload-size
# enforcement then proxies through to it, the same pattern as Scroll
# Recorder. See metadata/app/main.py for the actual read/strip logic.

@app.post("/api/metadata/process")
async def process_metadata(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api), __=Depends(require_module_enabled("metadata")),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"md:{ip}"):
        return JSONResponse(
            {"error": "Забагато запитів поспіль. Спробуйте пізніше."}, status_code=429
        )
    max_upload_mb = auth.get_max_upload_mb(db)
    return await proxy_helpers.proxy_upload(
        config.METADATA_URL, "/process", file, max_upload_mb * 1024 * 1024,
        os.path.join(config.DATA_DIR, "tmp_uploads"),
        too_large_message=f"Файл перевищує ліміт {max_upload_mb} МБ",
        unavailable_message="Модуль редактора метаданих недоступний",
    )


@app.get("/api/metadata/download/{token}")
async def download_clean_file(
    token: str, _=Depends(require_site_access_api), __=Depends(require_module_enabled("metadata")),
):
    return await proxy_helpers.proxy_file_stream(
        config.METADATA_URL, f"/download/{token}",
        unavailable_message="Модуль редактора метаданих недоступний",
    )


@app.get("/api/notifications/next")
def next_notification(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    username = request.session.get("site_username")
    if not username:
        return {}
    notif = (
        db.query(Notification)
        .filter(Notification.username == username)
        .order_by(Notification.created_at)
        .first()
    )
    if not notif:
        return {}
    return {"id": notif.id, "message": notif.message}


@app.post("/api/notifications/{notif_id}/dismiss")
def dismiss_notification(notif_id: str, request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    username = request.session.get("site_username")
    notif = db.get(Notification, notif_id)
    if notif and notif.username == username:
        db.delete(notif)
        db.commit()
    return {"ok": True}


# ---------------- First-run setup ----------------
# See auth.py's "First-run setup" section for what keeps this door shut. The
# one rule here: every path out of this handler that isn't a successful
# first admin ends in 404 or a re-rendered form - never in a redirect that
# could be replayed, and never in a hint about whether setup once existed.

@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db)):
    if auth.is_setup_completed(db):
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "setup.html",
        {"request": request, "error": None, "csrf_token": _issue_setup_csrf(request)},
    )


def _issue_setup_csrf(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session["setup_csrf"] = token
    return token


def _check_setup_csrf(request: Request, submitted: str) -> bool:
    expected = request.session.get("setup_csrf")
    if not expected or not submitted:
        return False
    return secrets.compare_digest(expected, submitted)


@app.post("/setup")
def setup_submit(
    request: Request,
    setup_token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    if auth.is_setup_completed(db):
        raise HTTPException(status_code=404)

    def again(message: str, status: int = 400):
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": message, "csrf_token": _issue_setup_csrf(request)},
            status_code=status,
        )

    # Same lockout the site login uses, so the token can't be ground down by
    # a script even though it's long enough that it shouldn't matter.
    ip = request.client.host if request.client else "unknown"
    key = f"setup:{ip}"
    locked, remaining = auth.check_lockout(key)
    if locked:
        minutes = max(1, remaining // 60)
        return again(f"Забагато спроб. Спробуйте ще раз через {minutes} хв.", 429)

    if not _check_setup_csrf(request, csrf_token):
        return again("Форма застаріла — оновіть сторінку і спробуйте ще раз.")

    if not auth.verify_setup_token(setup_token.strip()):
        auth.register_failed_attempt(key)
        return again(
            "Невірний код. Він друкується в логах контейнера при кожному запуску — "
            "перезапустіть стек, щоб отримати новий.",
            403,
        )

    username = username.strip()
    if not username:
        return again("Вкажіть логін.")
    if password != password_confirm:
        return again("Паролі не збігаються.")
    if len(password) < auth.MIN_ADMIN_PASSWORD_LENGTH:
        return again(f"Пароль має бути не коротшим за {auth.MIN_ADMIN_PASSWORD_LENGTH} символів.")

    user = auth.create_first_admin(db, username, password)
    if not user:
        # The door shut between the check at the top and here. Whoever holds
        # this form is not the owner of the account that now exists, so they
        # get what everyone else gets from now on.
        raise HTTPException(status_code=404)

    auth.register_successful_attempt(key)
    auth.clear_setup_token()
    request.session.pop("setup_csrf", None)
    request.session["site_access"] = True
    request.session["site_username"] = user.username
    auth.record_login(db, user.username)
    return RedirectResponse("/", status_code=303)


# ---------------- Site gate ----------------

@app.get("/site-login", response_class=HTMLResponse)
def site_login_form(request: Request, db: Session = Depends(get_db)):
    if not auth.is_site_gate_enabled(db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("site_login.html", {"request": request, "error": None})


@app.post("/site-login")
def site_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    key = f"site:{ip}"
    locked, remaining = auth.check_lockout(key)
    if locked:
        minutes = max(1, remaining // 60)
        return templates.TemplateResponse(
            "site_login.html",
            {"request": request, "error": f"Забагато спроб. Спробуйте ще раз через {minutes} хв."},
            status_code=429,
        )
    if auth.verify_site_credentials(db, username, password):
        auth.register_successful_attempt(key)
        auth.record_login(db, username)
        request.session["site_access"] = True
        request.session["site_username"] = username
        return RedirectResponse("/", status_code=303)
    auth.register_failed_attempt(key)
    return templates.TemplateResponse(
        "site_login.html", {"request": request, "error": "Невірний логін або пароль"}, status_code=401
    )


@app.get("/site-logout")
def site_logout(request: Request):
    request.session.pop("site_access", None)
    request.session.pop("site_username", None)
    return RedirectResponse("/site-login", status_code=303)


# ---------------- Admin ----------------
# Access is entirely user-based now (User.is_admin, granted from the Users
# tab) — there's no separate admin login. Whoever is logged into the site
# with an admin-flagged account gets /admin automatically.

@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/site-login", status_code=303)


def _page_param(request: Request, name: str) -> int:
    try:
        return max(1, int(request.query_params.get(name, 1)))
    except (TypeError, ValueError):
        return 1


# Static labels for the admin "Статистика" per-period activity table - the
# actual counts come from the downloader-converter module's own
# /admin/user-activity-summary (see stats.py there), this is just display
# text that never needed a DB round-trip of its own.
ACTIVITY_PERIODS = [("day", "24 г"), ("week", "7д"), ("month", "30д"), ("all", "весь час")]

HISTORY_PAGE_SIZE = 10


def _parse_row_dates(row: dict, fields=("created_at",)) -> dict:
    """Module admin endpoints send timestamps as ISO strings over JSON -
    admin.html's format_dt() global expects a real datetime, same as when
    these rows came straight from a local ORM query."""
    row = dict(row)
    for f in fields:
        if row.get(f):
            row[f] = datetime.fromisoformat(row[f])
    return row


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    downloader_converter_available = modules.is_module_available("downloader_converter")
    base_url = config.DOWNLOADER_CONVERTER_URL

    stats = await proxy_helpers.fetch_json(base_url, "/admin/stats", default={}) or {}
    total = stats.get("total", 0)
    finished = stats.get("finished", 0)
    errors = stats.get("errors", 0)
    total_size = stats.get("total_size", 0)
    cookies_used_count = stats.get("cookies_used_count", 0)
    by_source = stats.get("by_source", [])
    conversion_total = stats.get("conversion_total", 0)
    conversion_finished = stats.get("conversion_finished", 0)
    conversion_errors = stats.get("conversion_errors", 0)
    conversion_total_size = stats.get("conversion_total_size", 0)
    auto_conversion_count = stats.get("auto_conversion_count", 0)

    history_total_pages = max(1, -(-total // HISTORY_PAGE_SIZE))  # ceil division
    history_page = min(_page_param(request, "history_page"), history_total_pages)
    history_data = await proxy_helpers.fetch_json(
        base_url, "/admin/history/downloads", params={"page": history_page}, default={},
    ) or {}
    history = [_parse_row_dates(r) for r in history_data.get("items", [])]

    conversion_total_pages = max(1, -(-conversion_total // HISTORY_PAGE_SIZE))
    conversion_page = min(_page_param(request, "conversion_page"), conversion_total_pages)
    conversion_data = await proxy_helpers.fetch_json(
        base_url, "/admin/history/conversions", params={"page": conversion_page}, default={},
    ) or {}
    conversion_history = [_parse_row_dates(r) for r in conversion_data.get("items", [])]

    user_activity = await proxy_helpers.fetch_json(base_url, "/admin/user-activity-summary", default={}) or {}
    for key, _label in ACTIVITY_PERIODS:
        user_activity.setdefault(key, [])
    activity_periods = ACTIVITY_PERIODS

    sys_info = {
        "memory": sysinfo.get_memory_stats(),
        "cpu_temp": sysinfo.get_cpu_temperature(),
        "cpu_usage": sysinfo.get_cpu_usage_percent(),
        "network": sysinfo.get_persisted_network_stats(db),
    }

    retention_hours = auth.get_retention_hours(db)
    site_gate_enabled = auth.is_site_gate_enabled(db)
    users = auth.list_users(db)
    admin_user_count = sum(1 for u in users if u.is_admin)
    cleanup_interval_minutes = auth.get_cleanup_interval_minutes(db)
    max_concurrent_downloads = auth.get_max_concurrent_downloads(db)
    max_concurrent_conversions = auth.get_max_concurrent_conversions(db)
    max_upload_mb = auth.get_max_upload_mb(db)
    session_max_age_days = auth.get_session_max_age_days(db)
    proxy_url = auth.get_proxy_url(db)
    proxy_domains = ",".join(auth.get_proxy_domains(db))
    youtube_proxy_url = auth.get_youtube_proxy_url(db)
    timezone = auth.get_timezone(db)
    has_cookies = auth.has_cookies()
    module_enabled = {name: modules.is_module_enabled(name) for name in config.MODULE_URLS}

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "downloader_converter_available": downloader_converter_available,
            "site_gate_enabled": site_gate_enabled,
            "users": users,
            "admin_user_count": admin_user_count,
            "total": total,
            "finished": finished,
            "errors": errors,
            "total_size": total_size,
            "cookies_used_count": cookies_used_count,
            "by_source": by_source,
            "history": history,
            "history_page": history_page,
            "history_total_pages": history_total_pages,
            "conversion_total": conversion_total,
            "conversion_finished": conversion_finished,
            "conversion_errors": conversion_errors,
            "conversion_total_size": conversion_total_size,
            "auto_conversion_count": auto_conversion_count,
            "conversion_history": conversion_history,
            "conversion_page": conversion_page,
            "conversion_total_pages": conversion_total_pages,
            "user_activity": user_activity,
            "activity_periods": activity_periods,
            "sys_info": sys_info,
            "retention_hours": retention_hours,
            "cleanup_interval_minutes": cleanup_interval_minutes,
            "max_concurrent_downloads": max_concurrent_downloads,
            "max_concurrent_conversions": max_concurrent_conversions,
            "max_upload_mb": max_upload_mb,
            "session_max_age_days": session_max_age_days,
            "proxy_url": proxy_url,
            "proxy_domains": proxy_domains,
            "youtube_proxy_url": youtube_proxy_url,
            "timezone": timezone,
            "timezones": timeutil.COMMON_TIMEZONES,
            "has_cookies": has_cookies,
            "module_enabled": module_enabled,
        },
    )


@app.get("/admin/api/sysinfo")
def admin_sysinfo(db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    return {
        "memory": sysinfo.get_memory_stats(),
        "cpu_temp": sysinfo.get_cpu_temperature(),
        "cpu_usage": sysinfo.get_cpu_usage_percent(),
        "network": sysinfo.get_persisted_network_stats(db),
    }


@app.get("/admin/api/processes")
async def admin_processes(db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    """Same idea as /api/processes, but site-wide instead of scoped to one
    browser's client_id - lets an admin see what every regular user is up
    to. Admins' own jobs are deliberately left out: this tray is for keeping
    an eye on the userbase, not on other admins (or yourself)."""
    admin_usernames = [u.username for u in db.query(User).filter(User.is_admin.is_(True)).all()]
    return await proxy_helpers.fetch_json(
        config.DOWNLOADER_CONVERTER_URL, "/admin/processes",
        params={"exclude_usernames": ",".join(admin_usernames)}, default=[],
    )


@app.get("/admin/api/user-activity/{user_id}")
async def admin_user_activity(user_id: str, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    user = db.get(User, user_id)
    if not user:
        return JSONResponse({"error": "Користувача не знайдено"}, status_code=404)
    tz = auth.get_timezone(db)

    data = await proxy_helpers.fetch_json(
        config.DOWNLOADER_CONVERTER_URL, f"/admin/user-activity/{user.username}", default={},
    ) or {}
    return {
        "username": user.username,
        "created_at": timeutil.format_local(user.created_at, tz),
        "last_login": timeutil.format_local(user.last_login, tz) if user.last_login else None,
        "note": user.note or "",
        "downloads": [
            {
                "id": h["id"],
                "title": h["title"],
                "status": h["status"],
                "date": timeutil.format_local(datetime.fromisoformat(h["created_at"]), tz) if h["created_at"] else "",
                "size": sysinfo.format_bytes(h["filesize"]) if h["filesize"] else "",
            }
            for h in data.get("downloads", [])
        ],
        "conversions": [
            {
                "id": c["id"],
                "title": c["title"],
                "status": c["status"],
                "date": timeutil.format_local(datetime.fromisoformat(c["created_at"]), tz) if c["created_at"] else "",
                "size": sysinfo.format_bytes(c["filesize"]) if c["filesize"] else "",
            }
            for c in data.get("conversions", [])
        ],
    }


@app.post("/admin/api/user-activity/{user_id}/note")
def admin_save_user_note(
    user_id: str,
    note: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_admin_dep),
):
    user = db.get(User, user_id)
    if not user:
        return JSONResponse({"error": "Користувача не знайдено"}, status_code=404)
    user.note = note.strip()[:500] or None
    db.commit()
    return {"ok": True}


@app.get("/admin/api/errors/{kind}")
async def admin_errors(kind: str, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    if kind not in ("download", "conversion"):
        return JSONResponse({"error": "invalid kind"}, status_code=400)
    tz = auth.get_timezone(db)
    data = await proxy_helpers.fetch_json(config.DOWNLOADER_CONVERTER_URL, f"/admin/errors/{kind}", default={}) or {}
    return {
        "items": [
            {
                "id": r["id"],
                "title": r["title"],
                "url": r["url"],
                "username": r["username"],
                "date": timeutil.format_local(datetime.fromisoformat(r["created_at"]), tz) if r["created_at"] else "",
            }
            for r in data.get("items", [])
        ],
    }


@app.post("/admin/delete/{job_id}")
async def admin_delete(job_id: str, _=Depends(require_admin_dep)):
    await proxy_helpers.proxy_json(config.DOWNLOADER_CONVERTER_URL, "DELETE", f"/admin/jobs/download/{job_id}")
    # admin.html's own JS intercepts this form's submit and removes the row
    # in place (see "Delete a history row (no page reload)") - this
    # redirect only fires as the fallback for whatever reaches the server
    # without that JS having run (no-JS, an extension blocking fetch, a
    # race), and it belongs back on the tab the row was deleted from, not
    # wherever "stats" happens to be.
    return RedirectResponse("/admin?tab=history", status_code=303)


@app.post("/admin/delete-conversion/{job_id}")
async def admin_delete_conversion(job_id: str, _=Depends(require_admin_dep)):
    await proxy_helpers.proxy_json(config.DOWNLOADER_CONVERTER_URL, "DELETE", f"/admin/jobs/convert/{job_id}")
    return RedirectResponse("/admin?tab=history", status_code=303)


@app.post("/admin/users/add")
def admin_add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
    _=Depends(require_admin_dep),
):
    username = username.strip()
    if not username or not password:
        return RedirectResponse("/admin?tab=users&add_user_error=empty", status_code=303)
    if password != password_confirm:
        return RedirectResponse("/admin?tab=users&add_user_error=mismatch", status_code=303)
    if auth.username_exists(db, username):
        return RedirectResponse("/admin?tab=users&add_user_error=exists", status_code=303)
    auth.create_user(db, username, password)
    return RedirectResponse("/admin?tab=users&user_added=1", status_code=303)


@app.post("/admin/users/delete/{user_id}")
def admin_delete_user(user_id: str, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    ok = auth.delete_user(db, user_id)
    if not ok:
        return RedirectResponse("/admin?tab=users&user_error=last_admin", status_code=303)
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/users/reset-password/{user_id}")
def admin_reset_user_password(
    user_id: str,
    new_password: str = Form(...),
    new_password_confirm: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_admin_dep),
):
    if not new_password:
        return RedirectResponse("/admin?tab=users&user_error=empty", status_code=303)
    if new_password != new_password_confirm:
        return RedirectResponse("/admin?tab=users&user_error=mismatch", status_code=303)
    auth.reset_user_password(db, user_id, new_password)
    return RedirectResponse("/admin?tab=users&pw_reset=1", status_code=303)


@app.post("/admin/users/set-admin/{user_id}")
def admin_set_user_admin(
    user_id: str,
    make_admin: bool = Form(...),
    db: Session = Depends(get_db),
    _=Depends(require_admin_dep),
):
    ok = auth.set_user_admin(db, user_id, make_admin)
    if not ok:
        return RedirectResponse("/admin?tab=users&user_error=last_admin", status_code=303)
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/users/notify/{user_id}")
def admin_notify_user(
    user_id: str,
    message: str = Form(...),
    db: Session = Depends(get_db),
    _=Depends(require_admin_dep),
):
    message = message.strip()
    if not message:
        return RedirectResponse("/admin?tab=users&notify_error=empty", status_code=303)
    user = db.get(User, user_id)
    if not user:
        return RedirectResponse("/admin?tab=users", status_code=303)
    db.add(Notification(username=user.username, message=message[:2000]))
    db.commit()
    return RedirectResponse("/admin?tab=users&notify_sent=1", status_code=303)


@app.post("/admin/clear-ytdlp-cache")
async def admin_clear_ytdlp_cache(_=Depends(require_admin_dep)):
    await proxy_helpers.proxy_json(config.DOWNLOADER_CONVERTER_URL, "POST", "/admin/clear-ytdlp-cache")
    return RedirectResponse("/admin?tab=settings&cache_cleared=1", status_code=303)


@app.post("/admin/wipe-data")
async def admin_wipe_data(_=Depends(require_admin_dep)):
    await proxy_helpers.proxy_json(config.DOWNLOADER_CONVERTER_URL, "POST", "/admin/wipe-data")
    return RedirectResponse("/admin?tab=settings&data_wiped=1", status_code=303)


@app.post("/admin/delete-all-history")
async def admin_delete_all_history(_=Depends(require_admin_dep)):
    """Wipes every download/conversion row (and their files) from history -
    unlike /admin/wipe-data (which only frees disk space, leaving the rows
    behind as "expired"), this actually clears the Історія tables. Jobs
    still in flight are left alone rather than yanking the row out from
    under a background thread that's mid-update on it - they'll show up
    here once they finish (or get cancelled) like normal."""
    await proxy_helpers.proxy_json(config.DOWNLOADER_CONVERTER_URL, "POST", "/admin/delete-all-history")
    return RedirectResponse("/admin?tab=settings&history_deleted=1", status_code=303)


@app.post("/admin/settings")
def admin_settings(
    request: Request,
    retention_hours: int = Form(...),
    cleanup_interval_minutes: int = Form(...),
    max_concurrent_downloads: int = Form(...),
    max_concurrent_conversions: int = Form(...),
    max_upload_mb: int = Form(...),
    session_max_age_days: int = Form(...),
    proxy_url: str = Form(""),
    proxy_domains: str = Form(""),
    youtube_proxy_url: str = Form(""),
    timezone: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_admin_dep),
):
    auth.set_setting(db, "cleanup_hours", str(retention_hours))
    auth.set_setting(db, "cleanup_interval_minutes", str(cleanup_interval_minutes))
    auth.set_setting(db, "max_concurrent_downloads", str(max_concurrent_downloads))
    auth.set_setting(db, "max_concurrent_conversions", str(max_concurrent_conversions))
    auth.set_setting(db, "max_upload_mb", str(max_upload_mb))
    auth.set_setting(db, "session_max_age_days", str(session_max_age_days))
    auth.set_setting(db, "proxy_url", proxy_url.strip())
    auth.set_setting(db, "proxy_domains", proxy_domains.strip())
    auth.set_setting(db, "youtube_proxy_url", youtube_proxy_url.strip())
    if timeutil.is_valid_timezone(timezone):
        auth.set_setting(db, "timezone", timezone)

    modules.push_downloader_converter_config()
    return RedirectResponse("/admin?tab=settings&saved=1", status_code=303)


@app.post("/admin/modules")
def admin_modules(
    downloader_converter: bool = Form(False),
    metadata: bool = Form(False),
    scroll_recorder: bool = Form(False),
    db: Session = Depends(get_db),
    _=Depends(require_admin_dep),
):
    # An unchecked checkbox simply isn't sent by the browser, so every
    # module not named in the payload means "off" - each is listed as its
    # own Form(False) param above rather than parsed generically, exactly
    # so a module renamed/removed from config.MODULE_URLS can't silently
    # start being ignored here without FastAPI's own signature mismatch
    # complaining first.
    for name, enabled in (
        ("downloader_converter", downloader_converter),
        ("metadata", metadata),
        ("scroll_recorder", scroll_recorder),
    ):
        auth.set_module_enabled(db, name, enabled)
        modules.set_module_enabled(name, enabled)
    return RedirectResponse("/admin?tab=settings&modules_saved=1", status_code=303)


@app.get("/admin/api/proxy-status")
async def admin_proxy_status(which: str = "default", _=Depends(require_admin_dep)):
    return await proxy_helpers.fetch_json(
        config.DOWNLOADER_CONVERTER_URL, "/admin/proxy-status", params={"which": which},
        default={"configured": False, "active": False},
    )


@app.post("/admin/settings/cookies")
def admin_save_cookies(cookies_content: str = Form(...), _=Depends(require_admin_dep)):
    content = cookies_content.strip()
    if not content:
        return RedirectResponse("/admin?tab=settings&cookies_error=empty", status_code=303)
    auth.save_cookies(content)
    return RedirectResponse("/admin?tab=settings&cookies_saved=1", status_code=303)


@app.post("/admin/settings/cookies/clear")
def admin_clear_cookies(_=Depends(require_admin_dep)):
    auth.clear_cookies()
    return RedirectResponse("/admin?tab=settings&cookies_cleared=1", status_code=303)
