import os
import re
import shutil
import uuid

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import metadata_tool
from .cleanup import start_cleanup_thread

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# core already enforces the admin-configured upload-size limit before
# forwarding a file here (see app/proxy.py's proxy_upload) - this module has
# no DB/settings of its own, so it just trusts what core already sent.
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

app = FastAPI()


@app.on_event("startup")
def on_startup():
    start_cleanup_thread()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/process")
async def process(file: UploadFile = File(...)):
    token = uuid.uuid4().hex
    job_dir = os.path.join(DOWNLOAD_DIR, token)
    os.makedirs(job_dir, exist_ok=True)

    original_name = file.filename or "file"
    ext = os.path.splitext(original_name)[1][:15] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")
    with open(input_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    metadata = metadata_tool.read_metadata(input_path)
    if metadata is None:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "Не вдалося прочитати цей файл")

    clean_name = re.sub(r"[^\w\-. ]", "_", os.path.basename(original_name)).strip(" .") or "file"
    output_path = os.path.join(job_dir, f"clean_{clean_name}")
    ok, _err = metadata_tool.strip_metadata(input_path, output_path)
    if not ok:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "Не вдалося видалити метадані з цього формату файлу")

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


@app.get("/download/{token}")
def download(token: str, background_tasks: BackgroundTasks):
    if not TOKEN_RE.match(token):
        raise HTTPException(404, "Файл недоступний")
    job_dir = os.path.join(DOWNLOAD_DIR, token)
    if not os.path.isdir(job_dir):
        raise HTTPException(404, "Файл недоступний")
    candidates = [f for f in os.listdir(job_dir) if f.startswith("clean_")]
    if not candidates:
        raise HTTPException(404, "Файл недоступний")
    filepath = os.path.join(job_dir, candidates[0])
    filename = candidates[0][len("clean_"):]
    background_tasks.add_task(shutil.rmtree, job_dir, ignore_errors=True)
    return FileResponse(filepath, filename=filename)
