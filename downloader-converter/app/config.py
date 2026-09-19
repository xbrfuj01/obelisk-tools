import os
import shutil
import uuid

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
# Read-only bind mount of core's own DATA_DIR (see docker-compose.yml) - the
# only file this module needs from it is the cookies.txt the admin panel
# uploads via core, so a shared volume is simpler than round-tripping the
# whole file over the internal-config HTTP push on every change.
CORE_DATA_DIR = os.environ.get("CORE_DATA_DIR", "/core-data")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def get_cookies_path():
    """Returns a fresh, throwaway copy of the admin-uploaded cookies for
    this one extraction attempt, or None if none are configured. Never the
    CORE_DATA_DIR file directly: yt-dlp's --cookies doesn't just read that
    file, it dumps the (possibly refreshed) cookie jar back to the same
    path when it's done - and CORE_DATA_DIR is intentionally read-only
    from this container (see docker-compose.yml), so that write always
    failed with "Read-only file system". A per-call copy in this module's
    own writable DATA_DIR sidesteps that and, as a side effect, means two
    concurrent cookie-authenticated downloads never fight over the same
    file either. The caller is responsible for deleting it when done."""
    source = os.path.join(CORE_DATA_DIR, "youtube_cookies.txt")
    if not os.path.exists(source):
        return None
    scratch_dir = os.path.join(DATA_DIR, "tmp_cookies")
    os.makedirs(scratch_dir, exist_ok=True)
    dest = os.path.join(scratch_dir, f"{uuid.uuid4().hex}.txt")
    shutil.copyfile(source, dest)
    return dest
