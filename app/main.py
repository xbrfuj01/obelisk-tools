import os
import re
import shutil
import uuid
from datetime import datetime, timedelta

from fastapi import (
    BackgroundTasks, FastAPI, File, Request, Response, Form, Depends, HTTPException, UploadFile,
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, FileResponse, JSONResponse,
)
from starlette.staticfiles import StaticFiles as _StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import config
from .database import init_db, SessionLocal
from .models import Conversion, Download, Notification, User
from . import auth
from . import converter
from . import metadata_tool
from .downloader import (
    submit_job,
    _source_from_url,
    probe_qualities,
    is_url_allowed,
    clear_ytdlp_cache,
    parse_timecode,
    request_cancel as request_download_cancel,
    check_proxy_connection,
)
from .cleanup import start_cleanup_thread, wipe_all_data
from . import timeutil
from . import stats as stats_module
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


@app.exception_handler(auth.NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse("/site-login", status_code=303)


@app.exception_handler(auth.SiteNotAuthenticated)
async def site_not_authenticated_handler(request: Request, exc: auth.SiteNotAuthenticated):
    return RedirectResponse("/site-login", status_code=303)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


CLIENT_ID_COOKIE = "client_id"
CLIENT_ID_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days
RECENT_PAGE_SIZE = 10


def get_client_id(request: Request, response: Response) -> str:
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    if not client_id:
        client_id = uuid.uuid4().hex
        response.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return client_id


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


@app.on_event("startup")
def on_startup():
    start_cleanup_thread()
    modules.start_health_check_thread()


# ---------------- Public ----------------

@app.get("/", response_class=HTMLResponse)
def hub(request: Request, _=Depends(require_site_access_page)):
    return templates.TemplateResponse(
        "hub.html", {"request": request, "available": modules.available_modules()}
    )


@app.get("/downloader", response_class=HTMLResponse)
def downloader_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    is_new_client = not client_id
    if is_new_client:
        client_id = uuid.uuid4().hex

    recent = (
        db.query(Download)
        .filter(Download.client_id == client_id)
        .order_by(Download.created_at.desc())
        .limit(RECENT_PAGE_SIZE)
        .all()
    )
    resp = templates.TemplateResponse("downloader.html", {"request": request, "recent": recent})
    if is_new_client:
        resp.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


@app.get("/converter", response_class=HTMLResponse)
def converter_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    is_new_client = not client_id
    if is_new_client:
        client_id = uuid.uuid4().hex

    recent = (
        db.query(Conversion)
        .filter(Conversion.client_id == client_id)
        .order_by(Conversion.created_at.desc())
        .limit(RECENT_PAGE_SIZE)
        .all()
    )
    resp = templates.TemplateResponse(
        "converter.html",
        {"request": request, "recent": recent, "max_upload_mb": auth.get_max_upload_mb(db)},
    )
    if is_new_client:
        resp.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


@app.get("/metadata", response_class=HTMLResponse)
def metadata_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    return templates.TemplateResponse(
        "metadata.html", {"request": request, "max_upload_mb": auth.get_max_upload_mb(db)}
    )


@app.get("/scroll-recorder", response_class=HTMLResponse)
def scroll_recorder_page(request: Request, _=Depends(require_site_access_page)):
    if not modules.is_module_available("scroll_recorder"):
        return RedirectResponse("/", status_code=303)
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


@app.post("/api/scroll-recorder/jobs")
async def create_scroll_recorder_job(
    request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_api)
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
async def scroll_recorder_status(job_id: str, _=Depends(require_site_access_api)):
    return await _proxy_scroll_recorder("GET", f"/jobs/{job_id}")


@app.delete("/api/scroll-recorder/jobs/{job_id}")
async def cancel_scroll_recorder_job(job_id: str, _=Depends(require_site_access_api)):
    return await _proxy_scroll_recorder("DELETE", f"/jobs/{job_id}")


# Preview sessions open a real browser on the sidecar and hold it open
# while the user clicks elements to remove - /preview itself can take a
# while (a real page load), the rest are quick screenshot round-trips.
@app.post("/api/scroll-recorder/preview")
async def create_scroll_recorder_preview(
    request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_api)
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
    session_id: str, request: Request, _=Depends(require_site_access_api)
):
    body = await request.json()
    return await _proxy_scroll_recorder(
        "POST", f"/preview/{session_id}/remove", json_body=body, timeout=30
    )


@app.post("/api/scroll-recorder/preview/{session_id}/undo")
async def scroll_recorder_preview_undo(session_id: str, _=Depends(require_site_access_api)):
    return await _proxy_scroll_recorder("POST", f"/preview/{session_id}/undo", timeout=30)


@app.post("/api/scroll-recorder/preview/{session_id}/remove-header")
async def scroll_recorder_preview_remove_header(session_id: str, _=Depends(require_site_access_api)):
    return await _proxy_scroll_recorder("POST", f"/preview/{session_id}/remove-header", timeout=30)


@app.post("/api/scroll-recorder/preview/{session_id}/scroll")
async def scroll_recorder_preview_scroll(
    session_id: str, request: Request, _=Depends(require_site_access_api)
):
    body = await request.json()
    return await _proxy_scroll_recorder(
        "POST", f"/preview/{session_id}/scroll", json_body=body, timeout=30
    )


@app.post("/api/scroll-recorder/preview/{session_id}/record")
async def scroll_recorder_preview_record(
    session_id: str, request: Request, _=Depends(require_site_access_api)
):
    body = await request.json()
    return await _proxy_scroll_recorder(
        "POST", f"/preview/{session_id}/record", json_body=body, timeout=10
    )


@app.delete("/api/scroll-recorder/preview/{session_id}")
async def cancel_scroll_recorder_preview(session_id: str, _=Depends(require_site_access_api)):
    return await _proxy_scroll_recorder("DELETE", f"/preview/{session_id}", timeout=10)


@app.get("/api/scroll-recorder/jobs/{job_id}/file")
async def scroll_recorder_file(job_id: str, _=Depends(require_site_access_api)):
    return await proxy_helpers.proxy_file_stream(
        config.SCROLL_RECORDER_URL, f"/jobs/{job_id}/file", "scroll-recording.mp4", "video/mp4",
        unavailable_message="Модуль запису недоступний",
    )


@app.post("/api/download")
def create_download(
    request: Request,
    response: Response,
    url: str = Form(...),
    mode: str = Form("video"),
    quality: str = Form("best"),
    container: str = Form("mp4"),
    subtitle_lang: str = Form(""),
    premiere_compat: bool = Form(False),
    clip_start: str = Form(""),
    clip_end: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    client_id = get_client_id(request, response)
    ip = request.client.host if request.client else "unknown"

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "Некоректне посилання"}, status_code=400)
    if not is_url_allowed(url, db):
        return JSONResponse({"error": "Це посилання вказує на заборонену адресу"}, status_code=400)
    if not auth.check_download_rate_limit(f"dl:{ip}"):
        return JSONResponse(
            {"error": "Забагато завантажень поспіль. Спробуйте пізніше."}, status_code=429
        )
    if mode not in ("video", "video_only", "audio"):
        mode = "video"

    clip_start_sec = parse_timecode(clip_start)
    clip_end_sec = parse_timecode(clip_end)
    if clip_start and clip_start_sec is None:
        return JSONResponse({"error": "Некоректний початковий таймкод"}, status_code=400)
    if clip_end and clip_end_sec is None:
        return JSONResponse({"error": "Некоректний кінцевий таймкод"}, status_code=400)
    if clip_start_sec is not None and clip_end_sec is not None and clip_end_sec <= clip_start_sec:
        return JSONResponse({"error": "Кінцевий таймкод має бути більшим за початковий"}, status_code=400)

    job = Download(
        url=url,
        source=_source_from_url(url),
        mode=mode,
        quality=quality,
        container=container,
        subtitle_lang=subtitle_lang.strip() or None,
        premiere_compat=1 if premiere_compat else 0,
        clip_start=clip_start_sec,
        clip_end=clip_end_sec,
        status="queued",
        client_ip=request.client.host if request.client else None,
        client_id=client_id,
        username=request.session.get("site_username"),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    submit_job(job.id)
    return {"id": job.id}


@app.get("/api/status/{job_id}")
def job_status(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Download, job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "eta_seconds": job.eta_seconds,
        "title": job.title,
        "error": job.error_message,
        "filesize": job.filesize,
        "auto_convert_id": job.auto_convert_id,
        "premiere_compat": bool(job.premiere_compat),
    }


@app.post("/api/cancel/{job_id}")
def cancel_download(
    job_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    job = db.get(Download, job_id)
    client_id = get_client_id(request, response)
    if not job or job.client_id != client_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job.status not in ("queued", "downloading"):
        return JSONResponse({"error": "already finished"}, status_code=400)
    request_download_cancel(job_id)
    if job.status == "queued":
        # Still waiting for a slot - nothing running yet to catch the flag,
        # so reflect the cancellation immediately instead of waiting for its
        # turn to come up and notice on its own.
        job.status = "cancelled"
        job.finished_at = datetime.utcnow()
        db.commit()
    return {"ok": True}


@app.get("/api/formats")
def get_formats(request: Request, url: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"fmt:{ip}"):
        return JSONResponse(
            {"error": "Забагато запитів поспіль. Спробуйте пізніше."}, status_code=429
        )
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "Некоректне посилання"}, status_code=400)
    if not is_url_allowed(url, db):
        return JSONResponse({"error": "Це посилання вказує на заборонену адресу"}, status_code=400)
    try:
        return probe_qualities(url, db)
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=400)


@app.get("/api/recent")
def recent_jobs(
    request: Request,
    response: Response,
    page: int = 1,
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    client_id = get_client_id(request, response)
    page = max(1, page)
    total = db.query(func.count(Download.id)).filter(Download.client_id == client_id).scalar()
    total_pages = max(1, -(-total // RECENT_PAGE_SIZE))
    page = min(page, total_pages)
    rows = (
        db.query(Download)
        .filter(Download.client_id == client_id)
        .order_by(Download.created_at.desc())
        .offset((page - 1) * RECENT_PAGE_SIZE)
        .limit(RECENT_PAGE_SIZE)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title or r.url,
                "url": r.url,
                "status": r.status,
                "progress": r.progress,
                "source": r.source,
                "mode": r.mode,
                "filesize": r.filesize,
                "premiere_compat": bool(r.premiere_compat),
            }
            for r in rows
        ],
        "page": page,
        "total_pages": total_pages,
    }


CANCELLED_HIDE_AFTER_SECONDS = 30


def _hide_stale_cancelled(query, model):
    """A cancelled job is already fully cleaned up server-side the moment it
    happens - there's nothing left to act on, so leaving it sitting in the
    tray is just clutter. Kept visible for a short window so the person who
    just clicked cancel sees it register, then drops out on its own."""
    cutoff = datetime.utcnow() - timedelta(seconds=CANCELLED_HIDE_AFTER_SECONDS)
    return query.filter(or_(model.status != "cancelled", model.finished_at >= cutoff))


@app.get("/api/processes")
def processes(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    """Combined active + recent downloads/conversions for the top-right
    processes tray - kept in one feed (rather than two) so it reads as a
    single timeline regardless of which tool the job came from."""
    client_id = get_client_id(request, response)
    # "Прострочені" (expired - the file was cleaned up by the retention
    # sweep) jobs are done and gone, not something still worth downloading -
    # only ready-to-download or in-progress work belongs in this tray.
    downloads = _hide_stale_cancelled(
        db.query(Download).filter(Download.client_id == client_id, Download.status != "expired"),
        Download,
    ).order_by(Download.created_at.desc()).limit(20).all()
    conversions = _hide_stale_cancelled(
        db.query(Conversion).filter(Conversion.client_id == client_id, Conversion.status != "expired"),
        Conversion,
    ).order_by(Conversion.created_at.desc()).limit(20).all()
    items = [
        {
            "id": r.id,
            "kind": "download",
            "title": r.title or r.url,
            "status": r.status,
            "progress": r.progress,
            "eta_seconds": r.eta_seconds,
            "filesize": r.filesize,
            "created_at": r.created_at.isoformat(),
            "auto_convert_id": r.auto_convert_id,
        }
        for r in downloads
    ] + [
        {
            "id": r.id,
            "kind": "conversion",
            "title": r.original_filename or "video",
            "status": r.status,
            "progress": r.progress,
            "eta_seconds": r.eta_seconds,
            "filesize": r.filesize,
            "created_at": r.created_at.isoformat(),
        }
        for r in conversions
    ]
    items.sort(key=lambda it: it["created_at"], reverse=True)
    return items[:20]


@app.get("/api/file/{job_id}")
def download_file(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Download, job_id)
    if not job or job.status != "finished" or not job.filepath or not os.path.exists(job.filepath):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    filename = os.path.basename(job.filepath)
    return FileResponse(job.filepath, filename=filename)


# ---------------- Video converter ----------------

CONVERT_QUALITIES = {"high", "medium", "low"}
CONVERT_AUDIO_OPTIONS = {"aac", "original", "none"}


def _finalize_conversion(db, request, response, job_id, input_path, original_name, quality, audio_option):
    """Shared by /api/convert (browser upload) and /api/convert/from-download
    (reuses an already-downloaded file): probes the file already sitting at
    input_path, creates the Conversion row, and kicks off the background
    job. Returns (json_result, None) on success or (None, error_response)."""
    job_dir = os.path.dirname(input_path)
    if quality not in CONVERT_QUALITIES:
        quality = "high"
    if audio_option not in CONVERT_AUDIO_OPTIONS:
        audio_option = "original"

    info = converter.probe_input(input_path)
    if not info:
        shutil.rmtree(job_dir, ignore_errors=True)
        return None, JSONResponse({"error": "Не вдалося розпізнати відеофайл"}, status_code=400)

    client_id = get_client_id(request, response)
    job = Conversion(
        id=job_id,
        original_filename=original_name,
        input_summary=info["summary"],
        duration_seconds=info["duration"],
        quality=quality,
        audio_option=audio_option,
        status="queued",
        client_ip=request.client.host if request.client else None,
        client_id=client_id,
        username=request.session.get("site_username"),
    )
    db.add(job)
    db.commit()

    converter.submit_job(job_id, input_path, info)
    return {"id": job.id, "input_summary": job.input_summary, "duration_seconds": job.duration_seconds}, None


@app.post("/api/convert")
async def create_conversion(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    quality: str = Form("high"),
    audio_option: str = Form("original"),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"cv:{ip}"):
        return JSONResponse(
            {"error": "Забагато конвертацій поспіль. Спробуйте пізніше."}, status_code=429
        )

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "converts", job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_name = file.filename or "video"
    ext = os.path.splitext(original_name)[1][:10] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")

    max_bytes = auth.get_max_upload_mb(db) * 1024 * 1024
    total = 0
    too_large = False
    with open(input_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse(
            {"error": f"Файл перевищує ліміт {auth.get_max_upload_mb(db)} МБ"}, status_code=413
        )

    result, error_resp = _finalize_conversion(
        db, request, response, job_id, input_path, original_name, quality, audio_option
    )
    return error_resp if error_resp else result


@app.post("/api/convert/from-download/{download_id}")
def create_conversion_from_download(
    download_id: str,
    request: Request,
    response: Response,
    quality: str = Form("high"),
    audio_option: str = Form("original"),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    src = db.get(Download, download_id)
    if not src or src.status != "finished" or not src.filepath or not os.path.exists(src.filepath):
        return JSONResponse({"error": "Вихідний файл недоступний"}, status_code=404)
    if src.client_id != get_client_id(request, response):
        return JSONResponse({"error": "Вихідний файл недоступний"}, status_code=404)

    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"cv:{ip}"):
        return JSONResponse(
            {"error": "Забагато конвертацій поспіль. Спробуйте пізніше."}, status_code=429
        )

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "converts", job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_name = os.path.basename(src.filepath)
    ext = os.path.splitext(original_name)[1][:10] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")
    shutil.copyfile(src.filepath, input_path)

    result, error_resp = _finalize_conversion(
        db, request, response, job_id, input_path, original_name, quality, audio_option
    )
    return error_resp if error_resp else result


@app.get("/api/convert/status/{job_id}")
def conversion_status(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Conversion, job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "eta_seconds": job.eta_seconds,
        "input_summary": job.input_summary,
        "duration_seconds": job.duration_seconds,
        "error": job.error_message,
        "filesize": job.filesize,
    }


@app.post("/api/convert/cancel/{job_id}")
def cancel_conversion(
    job_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    job = db.get(Conversion, job_id)
    client_id = get_client_id(request, response)
    if not job or job.client_id != client_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job.status not in ("queued", "converting"):
        return JSONResponse({"error": "already finished"}, status_code=400)
    converter.request_cancel(job_id)
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = datetime.utcnow()
        db.commit()
    return {"ok": True}


@app.get("/api/convert/recent")
def recent_conversions(
    request: Request,
    response: Response,
    page: int = 1,
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    client_id = get_client_id(request, response)
    page = max(1, page)
    total = db.query(func.count(Conversion.id)).filter(Conversion.client_id == client_id).scalar()
    total_pages = max(1, -(-total // RECENT_PAGE_SIZE))
    page = min(page, total_pages)
    rows = (
        db.query(Conversion)
        .filter(Conversion.client_id == client_id)
        .order_by(Conversion.created_at.desc())
        .offset((page - 1) * RECENT_PAGE_SIZE)
        .limit(RECENT_PAGE_SIZE)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": r.original_filename or "video",
                "status": r.status,
                "progress": r.progress,
                "filesize": r.filesize,
            }
            for r in rows
        ],
        "page": page,
        "total_pages": total_pages,
    }


@app.get("/api/convert/file/{job_id}")
def conversion_file(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Conversion, job_id)
    if not job or job.status != "finished" or not job.filepath or not os.path.exists(job.filepath):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    filename = os.path.basename(job.filepath)
    return FileResponse(job.filepath, filename=filename)


# ---------------- Metadata editor ----------------
# No DB history here on purpose (unlike downloads/conversions) - this is a
# quick read-then-strip operation, not a background job. The cleaned file
# sits in a token-named temp dir just long enough to be downloaded once,
# cleaned up right after via a background task, with cleanup.py sweeping
# any abandoned ones (user never came back for the download) as a backstop.

METADATA_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


@app.post("/api/metadata/process")
async def process_metadata(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"md:{ip}"):
        return JSONResponse(
            {"error": "Забагато запитів поспіль. Спробуйте пізніше."}, status_code=429
        )

    token = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "metadata", token)
    os.makedirs(job_dir, exist_ok=True)

    original_name = file.filename or "file"
    ext = os.path.splitext(original_name)[1][:15] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")

    max_bytes = auth.get_max_upload_mb(db) * 1024 * 1024
    total = 0
    too_large = False
    with open(input_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse(
            {"error": f"Файл перевищує ліміт {auth.get_max_upload_mb(db)} МБ"}, status_code=413
        )

    metadata = metadata_tool.read_metadata(input_path)
    if metadata is None:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse({"error": "Не вдалося прочитати цей файл"}, status_code=400)

    clean_name = re.sub(r"[^\w\-. ]", "_", os.path.basename(original_name)).strip(" .") or "file"
    output_path = os.path.join(job_dir, f"clean_{clean_name}")
    ok, _err = metadata_tool.strip_metadata(input_path, output_path)
    if not ok:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse(
            {"error": "Не вдалося видалити метадані з цього формату файлу"}, status_code=400
        )

    after = metadata_tool.read_metadata(output_path)
    verified = after is not None
    classified = metadata_tool.classify_metadata(metadata, after, verified=verified)
    found_count = sum(1 for c in classified.values() if c["status"] != "absent")
    removable_count = sum(1 for c in classified.values() if c["status"] == "removable")
    return {
        "token": token,
        "filename": clean_name,
        "metadata": classified,
        "found_count": found_count,
        "removable_count": removable_count,
        "verified": verified,
    }


@app.get("/api/metadata/download/{token}")
def download_clean_file(
    token: str, background_tasks: BackgroundTasks, _=Depends(require_site_access_api)
):
    if not METADATA_TOKEN_RE.match(token):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    job_dir = os.path.join(config.DOWNLOAD_DIR, "metadata", token)
    if not os.path.isdir(job_dir):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    candidates = [f for f in os.listdir(job_dir) if f.startswith("clean_")]
    if not candidates:
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    filepath = os.path.join(job_dir, candidates[0])
    filename = candidates[0][len("clean_"):]
    background_tasks.add_task(shutil.rmtree, job_dir, ignore_errors=True)
    return FileResponse(filepath, filename=filename)


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


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    total = db.query(func.count(Download.id)).scalar()
    finished = db.query(func.count(Download.id)).filter(Download.status == "finished").scalar()
    errors = db.query(func.count(Download.id)).filter(Download.status == "error").scalar()
    total_size = (
        db.query(func.coalesce(func.sum(Download.filesize), 0))
        .filter(Download.status == "finished")
        .scalar()
    )
    cookies_used_count = db.query(func.count(Download.id)).filter(Download.used_cookies.is_(True)).scalar()

    by_source = (
        db.query(Download.source, func.count(Download.id))
        .filter(Download.status == "finished")
        .group_by(Download.source)
        .order_by(func.count(Download.id).desc())
        .limit(10)
        .all()
    )

    HISTORY_PAGE_SIZE = 10
    history_total_pages = max(1, -(-total // HISTORY_PAGE_SIZE))  # ceil division
    history_page = min(_page_param(request, "history_page"), history_total_pages)
    history = (
        db.query(Download)
        .order_by(Download.created_at.desc())
        .offset((history_page - 1) * HISTORY_PAGE_SIZE)
        .limit(HISTORY_PAGE_SIZE)
        .all()
    )

    conversion_total = db.query(func.count(Conversion.id)).scalar()
    conversion_finished = db.query(func.count(Conversion.id)).filter(Conversion.status == "finished").scalar()
    conversion_errors = db.query(func.count(Conversion.id)).filter(Conversion.status == "error").scalar()
    conversion_total_size = (
        db.query(func.coalesce(func.sum(Conversion.filesize), 0))
        .filter(Conversion.status == "finished")
        .scalar()
    )
    auto_conversion_count = db.query(func.count(Conversion.id)).filter(Conversion.is_auto.is_(True)).scalar()
    conversion_total_pages = max(1, -(-conversion_total // HISTORY_PAGE_SIZE))
    conversion_page = min(_page_param(request, "conversion_page"), conversion_total_pages)
    conversion_history = (
        db.query(Conversion)
        .order_by(Conversion.created_at.desc())
        .offset((conversion_page - 1) * HISTORY_PAGE_SIZE)
        .limit(HISTORY_PAGE_SIZE)
        .all()
    )

    user_activity = stats_module.user_activity(db)
    activity_periods = [(key, label) for key, label, _delta in stats_module.PERIODS]

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
    timezone = auth.get_timezone(db)
    has_cookies = auth.has_cookies()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
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
            "timezone": timezone,
            "timezones": timeutil.COMMON_TIMEZONES,
            "has_cookies": has_cookies,
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
def admin_processes(db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    """Same idea as /api/processes, but site-wide instead of scoped to one
    browser's client_id - lets an admin see what every regular user is up
    to. Admins' own jobs are deliberately left out: this tray is for keeping
    an eye on the userbase, not on other admins (or yourself)."""
    admin_usernames = [u.username for u in db.query(User).filter(User.is_admin.is_(True)).all()]
    downloads = _hide_stale_cancelled(
        db.query(Download).filter(
            Download.status != "expired",
            or_(Download.username.is_(None), Download.username.notin_(admin_usernames)),
        ),
        Download,
    ).order_by(Download.created_at.desc()).limit(50).all()
    conversions = _hide_stale_cancelled(
        db.query(Conversion).filter(
            Conversion.status != "expired",
            or_(Conversion.username.is_(None), Conversion.username.notin_(admin_usernames)),
        ),
        Conversion,
    ).order_by(Conversion.created_at.desc()).limit(50).all()
    items = [
        {
            "id": r.id,
            "kind": "download",
            "username": r.username or "—",
            "title": r.title or r.url,
            "status": r.status,
            "progress": r.progress,
            "eta_seconds": r.eta_seconds,
            "created_at": r.created_at.isoformat(),
        }
        for r in downloads
    ] + [
        {
            "id": r.id,
            "kind": "conversion",
            "username": r.username or "—",
            "title": r.original_filename or "video",
            "status": r.status,
            "progress": r.progress,
            "eta_seconds": r.eta_seconds,
            "created_at": r.created_at.isoformat(),
        }
        for r in conversions
    ]
    items.sort(key=lambda it: it["created_at"], reverse=True)
    return items[:50]


@app.get("/admin/api/user-activity/{user_id}")
def admin_user_activity(user_id: str, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    user = db.get(User, user_id)
    if not user:
        return JSONResponse({"error": "Користувача не знайдено"}, status_code=404)
    tz = auth.get_timezone(db)

    downloads = (
        db.query(Download)
        .filter(Download.username == user.username)
        .order_by(Download.created_at.desc())
        .limit(100)
        .all()
    )
    conversions = (
        db.query(Conversion)
        .filter(Conversion.username == user.username)
        .order_by(Conversion.created_at.desc())
        .limit(100)
        .all()
    )
    return {
        "username": user.username,
        "created_at": timeutil.format_local(user.created_at, tz),
        "last_login": timeutil.format_local(user.last_login, tz) if user.last_login else None,
        "note": user.note or "",
        "downloads": [
            {
                "id": h.id,
                "title": h.title or h.url,
                "status": h.status,
                "date": timeutil.format_local(h.created_at, tz),
                "size": sysinfo.format_bytes(h.filesize) if h.filesize else "",
            }
            for h in downloads
        ],
        "conversions": [
            {
                "id": c.id,
                "title": c.original_filename or "video",
                "status": c.status,
                "date": timeutil.format_local(c.created_at, tz),
                "size": sysinfo.format_bytes(c.filesize) if c.filesize else "",
            }
            for c in conversions
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
def admin_errors(kind: str, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    if kind not in ("download", "conversion"):
        return JSONResponse({"error": "invalid kind"}, status_code=400)
    tz = auth.get_timezone(db)
    model = Download if kind == "download" else Conversion
    rows = (
        db.query(model)
        .filter(model.status == "error")
        .order_by(model.created_at.desc())
        .limit(200)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": (r.title or r.url) if kind == "download" else (r.original_filename or "video"),
                "url": r.url if kind == "download" else None,
                "username": r.username or "—",
                "date": timeutil.format_local(r.created_at, tz),
            }
            for r in rows
        ],
    }


@app.post("/admin/delete/{job_id}")
def admin_delete(job_id: str, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    job = db.get(Download, job_id)
    if job:
        if job.filepath and os.path.exists(job.filepath):
            try:
                os.remove(job.filepath)
                parent = os.path.dirname(job.filepath)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
        db.delete(job)
        db.commit()
    return RedirectResponse("/admin?tab=stats", status_code=303)


@app.post("/admin/delete-conversion/{job_id}")
def admin_delete_conversion(job_id: str, db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    job = db.get(Conversion, job_id)
    if job:
        if job.filepath and os.path.exists(job.filepath):
            try:
                os.remove(job.filepath)
                parent = os.path.dirname(job.filepath)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
        db.delete(job)
        db.commit()
    return RedirectResponse("/admin?tab=stats", status_code=303)


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
def admin_clear_ytdlp_cache(_=Depends(require_admin_dep)):
    try:
        clear_ytdlp_cache()
    except Exception:
        pass
    return RedirectResponse("/admin?tab=settings&cache_cleared=1", status_code=303)


@app.post("/admin/wipe-data")
def admin_wipe_data(_=Depends(require_admin_dep)):
    try:
        wipe_all_data()
    except Exception:
        pass
    return RedirectResponse("/admin?tab=settings&data_wiped=1", status_code=303)


ACTIVE_JOB_STATUSES = ("queued", "downloading", "converting")


@app.post("/admin/delete-all-history")
def admin_delete_all_history(db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    """Wipes every download/conversion row (and their files) from history -
    unlike /admin/wipe-data (which only frees disk space, leaving the rows
    behind as "expired"), this actually clears the Історія tables. Jobs
    still in flight are left alone rather than yanking the row out from
    under a background thread that's mid-update on it - they'll show up
    here once they finish (or get cancelled) like normal."""
    for model, subdir in ((Download, None), (Conversion, "converts")):
        rows = db.query(model).filter(model.status.notin_(ACTIVE_JOB_STATUSES)).all()
        for job in rows:
            if job.filepath and os.path.exists(job.filepath):
                try:
                    os.remove(job.filepath)
                except OSError:
                    pass
            job_dir = os.path.join(config.DOWNLOAD_DIR, subdir, job.id) if subdir else os.path.join(config.DOWNLOAD_DIR, job.id)
            shutil.rmtree(job_dir, ignore_errors=True)
            db.delete(job)
    db.commit()
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
    if timeutil.is_valid_timezone(timezone):
        auth.set_setting(db, "timezone", timezone)

    return RedirectResponse("/admin?tab=settings&saved=1", status_code=303)


@app.get("/admin/api/proxy-status")
def admin_proxy_status(db: Session = Depends(get_db), _=Depends(require_admin_dep)):
    proxy_url = auth.get_proxy_url(db)
    if not proxy_url:
        return {"configured": False, "active": False}
    return {"configured": True, "active": check_proxy_connection(proxy_url)}


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
