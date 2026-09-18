import os
import shutil
import threading
import time

# The metadata editor keeps no DB history - its temp dirs are meant to live
# only until the user downloads the cleaned file (deleted right after via a
# background task in main.py). This is a backstop for ones nobody ever came
# back for. Not admin-configurable (unlike the download/convert retention
# window in the core site) - always was a fixed constant.
TEMP_MAX_AGE_HOURS = 1

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")


def _cleanup_temp_dirs():
    try:
        entries = os.listdir(DOWNLOAD_DIR)
    except OSError:
        return
    cutoff = time.time() - TEMP_MAX_AGE_HOURS * 3600
    for name in entries:
        path = os.path.join(DOWNLOAD_DIR, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _loop():
    while True:
        try:
            _cleanup_temp_dirs()
        except Exception:
            pass
        time.sleep(30 * 60)


def start_cleanup_thread():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
