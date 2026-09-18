import os

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
    path = os.path.join(CORE_DATA_DIR, "youtube_cookies.txt")
    return path if os.path.exists(path) else None
