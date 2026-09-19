import os
import shutil
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import config, settings_store
from . import converter
from . import stats as stats_module
from .cleanup import start_cleanup_thread, wipe_all_data
from .database import init_db, SessionLocal
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
from .models import Conversion, Download

init_db()

app = FastAPI()

RECENT_PAGE_SIZE = 10
HISTORY_PAGE_SIZE = 10
CANCELLED_HIDE_AFTER_SECONDS = 30
ACTIVE_JOB_STATUSES = ("queued", "downloading", "converting")
CONVERT_QUALITIES = {"high", "medium", "low"}
CONVERT_AUDIO_OPTIONS = {"aac", "original", "none"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    start_cleanup_thread()


@app.get("/health")
def health():
    return {"ok": True}


class ConfigPush(BaseModel):
    max_concurrent_downloads: Optional[int] = None
    max_concurrent_conversions: Optional[int] = None
    proxy_url: Optional[str] = None
    proxy_domains: Optional[list] = None
    youtube_proxy_url: Optional[str] = None
    retention_hours: Optional[int] = None
    cleanup_interval_minutes: Optional[int] = None


@app.post("/internal/config")
def push_config(body: ConfigPush):
    """Core resolves the real Setting rows and pushes them here - see
    app/modules.py on the core side. This module has no DB-backed settings
    of its own, just this in-memory cache (settings_store)."""
    data = {k: v for k, v in body.dict().items() if v is not None}
    settings_store.update(data)
    return {"ok": True}


def _hide_stale_cancelled(query, model):
    cutoff = datetime.utcnow() - timedelta(seconds=CANCELLED_HIDE_AFTER_SECONDS)
    return query.filter(or_(model.status != "cancelled", model.finished_at >= cutoff))


# ---------------- Downloader ----------------

class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"
    quality: str = "best"
    container: str = "mp4"
    subtitle_lang: str = ""
    premiere_compat: bool = False
    clip_start: str = ""
    clip_end: str = ""
    client_id: str
    client_ip: Optional[str] = None
    username: Optional[str] = None


@app.post("/jobs/download")
def create_download(body: DownloadRequest, db: Session = Depends(get_db)):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Некоректне посилання")
    if not is_url_allowed(url):
        raise HTTPException(400, "Це посилання вказує на заборонену адресу")

    mode = body.mode if body.mode in ("video", "video_only", "audio") else "video"

    clip_start_sec = parse_timecode(body.clip_start)
    clip_end_sec = parse_timecode(body.clip_end)
    if body.clip_start and clip_start_sec is None:
        raise HTTPException(400, "Некоректний початковий таймкод")
    if body.clip_end and clip_end_sec is None:
        raise HTTPException(400, "Некоректний кінцевий таймкод")
    if clip_start_sec is not None and clip_end_sec is not None and clip_end_sec <= clip_start_sec:
        raise HTTPException(400, "Кінцевий таймкод має бути більшим за початковий")

    job = Download(
        url=url,
        source=_source_from_url(url),
        mode=mode,
        quality=body.quality,
        container=body.container,
        subtitle_lang=body.subtitle_lang.strip() or None,
        premiere_compat=1 if body.premiere_compat else 0,
        clip_start=clip_start_sec,
        clip_end=clip_end_sec,
        status="queued",
        client_ip=body.client_ip,
        client_id=body.client_id,
        username=body.username,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    submit_job(job.id)
    return {"id": job.id}


@app.get("/jobs/download/{job_id}")
def job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Download, job_id)
    if not job:
        raise HTTPException(404, "not found")
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


class CancelRequest(BaseModel):
    client_id: str


@app.post("/jobs/download/{job_id}/cancel")
def cancel_download(job_id: str, body: CancelRequest, db: Session = Depends(get_db)):
    job = db.get(Download, job_id)
    if not job or job.client_id != body.client_id:
        raise HTTPException(404, "not found")
    if job.status not in ("queued", "downloading"):
        raise HTTPException(400, "already finished")
    request_download_cancel(job_id)
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = datetime.utcnow()
        db.commit()
    return {"ok": True}


@app.get("/jobs/download")
def recent_jobs(client_id: str, page: int = 1, db: Session = Depends(get_db)):
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
                "id": r.id, "title": r.title or r.url, "url": r.url, "status": r.status,
                "progress": r.progress, "source": r.source, "mode": r.mode,
                "filesize": r.filesize, "premiere_compat": bool(r.premiere_compat),
            }
            for r in rows
        ],
        "page": page, "total_pages": total_pages,
    }


@app.get("/jobs/download/{job_id}/file")
def download_file(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Download, job_id)
    if not job or job.status != "finished" or not job.filepath or not os.path.exists(job.filepath):
        raise HTTPException(404, "Файл недоступний")
    return FileResponse(job.filepath, filename=os.path.basename(job.filepath))


@app.get("/jobs/formats")
def get_formats(url: str):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Некоректне посилання")
    if not is_url_allowed(url):
        raise HTTPException(400, "Це посилання вказує на заборонену адресу")
    try:
        return probe_qualities(url)
    except Exception as e:
        raise HTTPException(400, str(e)[:300])


# ---------------- Converter ----------------

def _finalize_conversion(db, job_id, input_path, original_name, quality, audio_option,
                          client_ip, client_id, username):
    job_dir = os.path.dirname(input_path)
    if quality not in CONVERT_QUALITIES:
        quality = "high"
    if audio_option not in CONVERT_AUDIO_OPTIONS:
        audio_option = "original"

    info = converter.probe_input(input_path)
    if not info:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "Не вдалося розпізнати відеофайл")

    job = Conversion(
        id=job_id,
        original_filename=original_name,
        input_summary=info["summary"],
        duration_seconds=info["duration"],
        quality=quality,
        audio_option=audio_option,
        status="queued",
        client_ip=client_ip,
        client_id=client_id,
        username=username,
    )
    db.add(job)
    db.commit()

    converter.submit_job(job_id, input_path, info)
    return {"id": job.id, "input_summary": job.input_summary, "duration_seconds": job.duration_seconds}


@app.post("/jobs/convert")
async def create_conversion(
    file: UploadFile = File(...),
    quality: str = Form("high"),
    audio_option: str = Form("original"),
    client_id: str = Form(...),
    client_ip: str = Form(""),
    username: str = Form(""),
    db: Session = Depends(get_db),
):
    # Upload size was already enforced by core before this request was ever
    # forwarded (see app/proxy.py's proxy_upload) - this module has no
    # admin-configured settings of its own to re-check it against.
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "converts", job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_name = file.filename or "video"
    ext = os.path.splitext(original_name)[1][:10] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")
    with open(input_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    return _finalize_conversion(
        db, job_id, input_path, original_name, quality, audio_option,
        client_ip or None, client_id, username or None,
    )


class ConvertFromDownloadRequest(BaseModel):
    quality: str = "high"
    audio_option: str = "original"
    client_id: str
    client_ip: Optional[str] = None
    username: Optional[str] = None


@app.post("/jobs/convert/from-download/{download_id}")
def create_conversion_from_download(download_id: str, body: ConvertFromDownloadRequest, db: Session = Depends(get_db)):
    src = db.get(Download, download_id)
    if not src or src.status != "finished" or not src.filepath or not os.path.exists(src.filepath):
        raise HTTPException(404, "Вихідний файл недоступний")
    if src.client_id != body.client_id:
        raise HTTPException(404, "Вихідний файл недоступний")

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "converts", job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_name = os.path.basename(src.filepath)
    ext = os.path.splitext(original_name)[1][:10] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")
    shutil.copyfile(src.filepath, input_path)

    return _finalize_conversion(
        db, job_id, input_path, original_name, body.quality, body.audio_option,
        body.client_ip, body.client_id, body.username,
    )


@app.get("/jobs/convert/{job_id}")
def conversion_status(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Conversion, job_id)
    if not job:
        raise HTTPException(404, "not found")
    return {
        "id": job.id, "status": job.status, "progress": job.progress, "eta_seconds": job.eta_seconds,
        "input_summary": job.input_summary, "duration_seconds": job.duration_seconds,
        "error": job.error_message, "filesize": job.filesize,
    }


@app.post("/jobs/convert/{job_id}/cancel")
def cancel_conversion(job_id: str, body: CancelRequest, db: Session = Depends(get_db)):
    job = db.get(Conversion, job_id)
    if not job or job.client_id != body.client_id:
        raise HTTPException(404, "not found")
    if job.status not in ("queued", "converting"):
        raise HTTPException(400, "already finished")
    converter.request_cancel(job_id)
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = datetime.utcnow()
        db.commit()
    return {"ok": True}


@app.get("/jobs/convert")
def recent_conversions(client_id: str, page: int = 1, db: Session = Depends(get_db)):
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
            {"id": r.id, "title": r.original_filename or "video", "status": r.status,
             "progress": r.progress, "filesize": r.filesize}
            for r in rows
        ],
        "page": page, "total_pages": total_pages,
    }


@app.get("/jobs/convert/{job_id}/file")
def conversion_file(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Conversion, job_id)
    if not job or job.status != "finished" or not job.filepath or not os.path.exists(job.filepath):
        raise HTTPException(404, "Файл недоступний")
    return FileResponse(job.filepath, filename=os.path.basename(job.filepath))


# ---------------- Combined tray ----------------

@app.get("/jobs/processes")
def processes(client_id: str, db: Session = Depends(get_db)):
    downloads = _hide_stale_cancelled(
        db.query(Download).filter(Download.client_id == client_id, Download.status != "expired"), Download,
    ).order_by(Download.created_at.desc()).limit(20).all()
    conversions = _hide_stale_cancelled(
        db.query(Conversion).filter(Conversion.client_id == client_id, Conversion.status != "expired"), Conversion,
    ).order_by(Conversion.created_at.desc()).limit(20).all()
    items = [
        {"id": r.id, "kind": "download", "title": r.title or r.url, "status": r.status,
         "progress": r.progress, "eta_seconds": r.eta_seconds, "filesize": r.filesize,
         "created_at": r.created_at.isoformat(), "auto_convert_id": r.auto_convert_id}
        for r in downloads
    ] + [
        {"id": r.id, "kind": "conversion", "title": r.original_filename or "video", "status": r.status,
         "progress": r.progress, "eta_seconds": r.eta_seconds, "filesize": r.filesize,
         "created_at": r.created_at.isoformat()}
        for r in conversions
    ]
    items.sort(key=lambda it: it["created_at"], reverse=True)
    return items[:20]


# ---------------- Admin ----------------
# No auth here - core's own admin session check already ran before any of
# these routes get called (see app/main.py's admin_dashboard/_admin_dep).

@app.get("/admin/stats")
def admin_stats(db: Session = Depends(get_db)):
    total = db.query(func.count(Download.id)).scalar()
    finished = db.query(func.count(Download.id)).filter(Download.status == "finished").scalar()
    errors = db.query(func.count(Download.id)).filter(Download.status == "error").scalar()
    total_size = db.query(func.coalesce(func.sum(Download.filesize), 0)).filter(Download.status == "finished").scalar()
    cookies_used_count = db.query(func.count(Download.id)).filter(Download.used_cookies.is_(True)).scalar()
    by_source = [
        [source, count] for source, count in (
            db.query(Download.source, func.count(Download.id))
            .filter(Download.status == "finished")
            .group_by(Download.source)
            .order_by(func.count(Download.id).desc())
            .limit(10)
            .all()
        )
    ]
    conversion_total = db.query(func.count(Conversion.id)).scalar()
    conversion_finished = db.query(func.count(Conversion.id)).filter(Conversion.status == "finished").scalar()
    conversion_errors = db.query(func.count(Conversion.id)).filter(Conversion.status == "error").scalar()
    conversion_total_size = db.query(func.coalesce(func.sum(Conversion.filesize), 0)).filter(Conversion.status == "finished").scalar()
    auto_conversion_count = db.query(func.count(Conversion.id)).filter(Conversion.is_auto.is_(True)).scalar()
    return {
        "total": total, "finished": finished, "errors": errors, "total_size": total_size,
        "cookies_used_count": cookies_used_count, "by_source": by_source,
        "conversion_total": conversion_total, "conversion_finished": conversion_finished,
        "conversion_errors": conversion_errors, "conversion_total_size": conversion_total_size,
        "auto_conversion_count": auto_conversion_count,
    }


@app.get("/admin/history/downloads")
def admin_history_downloads(page: int = 1, db: Session = Depends(get_db)):
    total = db.query(func.count(Download.id)).scalar()
    total_pages = max(1, -(-total // HISTORY_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    rows = (
        db.query(Download).order_by(Download.created_at.desc())
        .offset((page - 1) * HISTORY_PAGE_SIZE).limit(HISTORY_PAGE_SIZE).all()
    )
    return {
        "items": [
            {
                "id": r.id, "status": r.status, "title": r.title, "url": r.url, "source": r.source,
                "mode": r.mode, "used_cookies": bool(r.used_cookies), "username": r.username,
                "created_at": r.created_at.isoformat() if r.created_at else None, "filesize": r.filesize,
            }
            for r in rows
        ],
        "page": page, "total_pages": total_pages,
    }


@app.get("/admin/history/conversions")
def admin_history_conversions(page: int = 1, db: Session = Depends(get_db)):
    total = db.query(func.count(Conversion.id)).scalar()
    total_pages = max(1, -(-total // HISTORY_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    rows = (
        db.query(Conversion).order_by(Conversion.created_at.desc())
        .offset((page - 1) * HISTORY_PAGE_SIZE).limit(HISTORY_PAGE_SIZE).all()
    )
    return {
        "items": [
            {
                "id": r.id, "status": r.status, "original_filename": r.original_filename, "quality": r.quality,
                "audio_option": r.audio_option, "is_auto": bool(r.is_auto), "username": r.username,
                "created_at": r.created_at.isoformat() if r.created_at else None, "filesize": r.filesize,
            }
            for r in rows
        ],
        "page": page, "total_pages": total_pages,
    }


@app.get("/admin/user-activity-summary")
def admin_user_activity_summary(db: Session = Depends(get_db)):
    return stats_module.user_activity(db)


@app.get("/admin/user-activity/{username}")
def admin_user_activity(username: str, db: Session = Depends(get_db)):
    downloads = (
        db.query(Download).filter(Download.username == username)
        .order_by(Download.created_at.desc()).limit(100).all()
    )
    conversions = (
        db.query(Conversion).filter(Conversion.username == username)
        .order_by(Conversion.created_at.desc()).limit(100).all()
    )
    return {
        "downloads": [
            {"id": h.id, "title": h.title or h.url, "status": h.status,
             "created_at": h.created_at.isoformat() if h.created_at else None, "filesize": h.filesize}
            for h in downloads
        ],
        "conversions": [
            {"id": c.id, "title": c.original_filename or "video", "status": c.status,
             "created_at": c.created_at.isoformat() if c.created_at else None, "filesize": c.filesize}
            for c in conversions
        ],
    }


@app.get("/admin/processes")
def admin_processes(exclude_usernames: str = "", db: Session = Depends(get_db)):
    """Same idea as /jobs/processes, but site-wide instead of scoped to one
    client_id - core passes the admin usernames to exclude (it owns the
    User table), since this tray is for keeping an eye on the userbase, not
    on other admins."""
    admin_usernames = [u for u in exclude_usernames.split(",") if u]
    downloads = _hide_stale_cancelled(
        db.query(Download).filter(
            Download.status != "expired",
            or_(Download.username.is_(None), Download.username.notin_(admin_usernames)),
        ), Download,
    ).order_by(Download.created_at.desc()).limit(50).all()
    conversions = _hide_stale_cancelled(
        db.query(Conversion).filter(
            Conversion.status != "expired",
            or_(Conversion.username.is_(None), Conversion.username.notin_(admin_usernames)),
        ), Conversion,
    ).order_by(Conversion.created_at.desc()).limit(50).all()
    items = [
        {"id": r.id, "kind": "download", "username": r.username or "—", "title": r.title or r.url,
         "status": r.status, "progress": r.progress, "eta_seconds": r.eta_seconds,
         "created_at": r.created_at.isoformat()}
        for r in downloads
    ] + [
        {"id": r.id, "kind": "conversion", "username": r.username or "—", "title": r.original_filename or "video",
         "status": r.status, "progress": r.progress, "eta_seconds": r.eta_seconds,
         "created_at": r.created_at.isoformat()}
        for r in conversions
    ]
    items.sort(key=lambda it: it["created_at"], reverse=True)
    return items[:50]


@app.get("/admin/errors/{kind}")
def admin_errors(kind: str, db: Session = Depends(get_db)):
    if kind not in ("download", "conversion"):
        raise HTTPException(400, "invalid kind")
    model = Download if kind == "download" else Conversion
    rows = db.query(model).filter(model.status == "error").order_by(model.created_at.desc()).limit(200).all()
    return {
        "items": [
            {
                "id": r.id,
                "title": (r.title or r.url) if kind == "download" else (r.original_filename or "video"),
                "url": r.url if kind == "download" else None,
                "username": r.username or "—",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@app.get("/admin/proxy-status")
def admin_proxy_status(which: str = "default"):
    key = "youtube_proxy_url" if which == "youtube" else "proxy_url"
    proxy_url = settings_store.get(key, "")
    if not proxy_url:
        return {"configured": False, "active": False}
    return {"configured": True, "active": check_proxy_connection(proxy_url)}


@app.delete("/admin/jobs/download/{job_id}")
def admin_delete_download(job_id: str, db: Session = Depends(get_db)):
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
    return {"ok": True}


@app.delete("/admin/jobs/convert/{job_id}")
def admin_delete_conversion(job_id: str, db: Session = Depends(get_db)):
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
    return {"ok": True}


@app.post("/admin/clear-ytdlp-cache")
def admin_clear_ytdlp_cache():
    try:
        clear_ytdlp_cache()
    except Exception:
        pass
    return {"ok": True}


@app.post("/admin/wipe-data")
def admin_wipe_data():
    try:
        wipe_all_data()
    except Exception:
        pass
    return {"ok": True}


@app.post("/admin/delete-all-history")
def admin_delete_all_history(db: Session = Depends(get_db)):
    """Wipes every download/conversion row (and their files) from history -
    unlike /admin/wipe-data (which only frees disk space, leaving the rows
    behind as "expired"), this actually clears the Історія tables. Jobs
    still in flight are left alone rather than yanking the row out from
    under a background thread that's mid-update on it."""
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
    return {"ok": True}
