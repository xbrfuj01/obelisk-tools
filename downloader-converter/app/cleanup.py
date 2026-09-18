import os
import shutil
import threading
import time
from datetime import datetime, timedelta

from . import config, settings_store
from .database import SessionLocal
from .models import Conversion, Download


def run_cleanup_once():
    """The scheduled sweep: removes finished/errored jobs older than the
    configured retention window. Runs automatically on a timer - see
    wipe_all_data() below for the manual "delete everything now" button."""
    retention_hours = settings_store.get("retention_hours", 24)
    _sweep_jobs(max_age_hours=retention_hours)


def wipe_all_data():
    """Manual "clean up data" button: removes every finished/errored job's
    files right now, regardless of age - the scheduled sweep above already
    handles the time-based cleanup, so this is purely for "free up disk space
    immediately" rather than a second, redundant retention policy."""
    _sweep_jobs(max_age_hours=None)


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


def _loop():
    while True:
        try:
            run_cleanup_once()
        except Exception:
            pass
        interval_minutes = settings_store.get("cleanup_interval_minutes", 30)
        time.sleep(interval_minutes * 60)


def start_cleanup_thread():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
