import os
import shutil
import threading
import time
from datetime import datetime, timedelta

from . import auth, config
from .database import SessionLocal
from .models import Conversion, Download

# The metadata editor keeps no DB history - its temp dirs are meant to live
# only until the user downloads the cleaned file (deleted right after via a
# background task). This is a backstop for ones nobody ever came back for.
METADATA_TEMP_MAX_AGE_HOURS = 1


def run_cleanup_once():
    """The scheduled sweep: removes finished/errored jobs older than the
    configured retention window. Runs automatically on a timer - see
    wipe_all_data() below for the manual "delete everything now" button."""
    db = SessionLocal()
    try:
        retention_hours = auth.get_retention_hours(db)
    finally:
        db.close()
    _sweep_jobs(max_age_hours=retention_hours)
    _cleanup_metadata_temp_dirs()


def wipe_all_data():
    """Manual "clean up data" button: removes every finished/errored job's
    files right now, regardless of age - the scheduled sweep above already
    handles the time-based cleanup, so this is purely for "free up disk space
    immediately" rather than a second, redundant retention policy."""
    _sweep_jobs(max_age_hours=None)
    _cleanup_metadata_temp_dirs()


def _sweep_jobs(max_age_hours):
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours) if max_age_hours is not None else None
        # "error" is included alongside "finished": a failed download/conversion
        # still leaves its whole working directory behind (partial fragments,
        # a half-written output file, ...) since nothing else ever removes it -
        # only job.filepath (never set on failure) was swept before, so those
        # never actually got cleaned up.
        for model, subdir in ((Download, None), (Conversion, "converts")):
            query = db.query(model).filter(model.status.in_(("finished", "error", "cancelled")))
            if cutoff is not None:
                query = query.filter(model.finished_at.isnot(None)).filter(model.finished_at < cutoff)
            for job in query.all():
                job_dir = os.path.join(config.DOWNLOAD_DIR, subdir, job.id) if subdir else os.path.join(config.DOWNLOAD_DIR, job.id)
                shutil.rmtree(job_dir, ignore_errors=True)
                if job.status == "finished":
                    job.filepath = None
                    job.status = "expired"
        db.commit()
    finally:
        db.close()


def _cleanup_metadata_temp_dirs():
    base = os.path.join(config.DOWNLOAD_DIR, "metadata")
    try:
        entries = os.listdir(base)
    except OSError:
        return
    cutoff = time.time() - METADATA_TEMP_MAX_AGE_HOURS * 3600
    for name in entries:
        path = os.path.join(base, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _loop():
    while True:
        db = SessionLocal()
        try:
            interval_minutes = auth.get_cleanup_interval_minutes(db)
        finally:
            db.close()
        try:
            run_cleanup_once()
        except Exception:
            pass
        time.sleep(interval_minutes * 60)


def start_cleanup_thread():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
