import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import recorder

app = FastAPI()

ASPECT_RATIOS = set(recorder.VIEWPORTS)


@app.get("/health")
def health():
    return {"ok": True}


def _validate_common(url: str, aspect_ratio: str, device: str):
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "URL має починатися з http:// або https://")
    if device not in recorder.DEVICE_MODES:
        raise HTTPException(400, "Невідомий режим пристрою")
    # Mobile mode records at the emulated phone's own natural shape (see
    # recorder.py's MOBILE_DEVICE_NAME comment) - aspect_ratio only applies
    # to desktop mode, so it isn't validated/used otherwise.
    if device == "desktop" and aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(400, "Невідоме співвідношення сторін")


def _validate_duration_framerate(duration_seconds: int, framerate: int):
    if not (recorder.MIN_DURATION_SECONDS <= duration_seconds <= recorder.MAX_DURATION_SECONDS):
        raise HTTPException(
            400,
            f"Тривалість має бути від {recorder.MIN_DURATION_SECONDS} "
            f"до {recorder.MAX_DURATION_SECONDS} секунд",
        )
    if framerate not in recorder.FRAME_RATES:
        raise HTTPException(400, "Невідомий фреймрейт")


class JobRequest(BaseModel):
    url: str
    aspect_ratio: str
    device: str = "desktop"
    duration_seconds: int
    framerate: int
    block_ads: bool = False
    proxy_url: Optional[str] = None
    start_fraction: float = 0.0
    end_fraction: float = 1.0


@app.post("/jobs")
def create_job(body: JobRequest):
    _validate_common(body.url, body.aspect_ratio, body.device)
    _validate_duration_framerate(body.duration_seconds, body.framerate)
    job_id = recorder.create_job(
        body.url, body.aspect_ratio, body.device, body.duration_seconds, body.framerate,
        body.block_ads, body.proxy_url, body.start_fraction, body.end_fraction,
    )
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = recorder.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    return job


@app.get("/jobs/{job_id}/file")
def job_file(job_id: str):
    path = recorder.job_file_path(job_id)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "not ready")
    return FileResponse(path, media_type="video/mp4", filename="scroll-recording.mp4")


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str):
    recorder.request_cancel(job_id)
    return {"ok": True}


class PreviewRequest(BaseModel):
    url: str
    aspect_ratio: str
    device: str = "desktop"
    block_ads: bool = False
    proxy_url: Optional[str] = None


@app.post("/preview")
def create_preview(body: PreviewRequest):
    _validate_common(body.url, body.aspect_ratio, body.device)
    try:
        session_id, screenshot, width, height, y, page_height = recorder.create_preview(
            body.url, body.aspect_ratio, body.device, body.block_ads, body.proxy_url
        )
    except RuntimeError as exc:
        raise HTTPException(429, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Не вдалося відкрити сторінку: {exc}")
    return {
        "session_id": session_id, "screenshot": screenshot, "width": width, "height": height,
        "y": y, "page_height": page_height,
    }


class PointRequest(BaseModel):
    x: float
    y: float


@app.post("/preview/{session_id}/remove")
def remove_element(session_id: str, body: PointRequest):
    try:
        screenshot = recorder.remove_at_point(session_id, body.x, body.y)
    except KeyError:
        raise HTTPException(404, "session not found")
    return {"screenshot": screenshot}


@app.post("/preview/{session_id}/undo")
def undo_element(session_id: str):
    try:
        screenshot = recorder.undo_last(session_id)
    except KeyError:
        raise HTTPException(404, "session not found")
    return {"screenshot": screenshot}


@app.post("/preview/{session_id}/remove-header")
def remove_header(session_id: str):
    try:
        screenshot = recorder.remove_header(session_id)
    except KeyError:
        raise HTTPException(404, "session not found")
    return {"screenshot": screenshot}


class ScrollRequest(BaseModel):
    delta_y: float


@app.post("/preview/{session_id}/scroll")
def scroll_preview(session_id: str, body: ScrollRequest):
    try:
        return recorder.scroll_preview(session_id, body.delta_y)
    except KeyError:
        raise HTTPException(404, "session not found")


class RecordFromPreviewRequest(BaseModel):
    duration_seconds: int
    framerate: int
    start_fraction: float = 0.0
    end_fraction: float = 1.0


@app.post("/preview/{session_id}/record")
def record_from_preview(session_id: str, body: RecordFromPreviewRequest):
    _validate_duration_framerate(body.duration_seconds, body.framerate)
    try:
        job_id = recorder.start_recording_from_preview(
            session_id, body.duration_seconds, body.framerate,
            body.start_fraction, body.end_fraction,
        )
    except KeyError:
        raise HTTPException(404, "session not found")
    return {"job_id": job_id}


@app.delete("/preview/{session_id}")
def cancel_preview(session_id: str):
    recorder.close_preview(session_id)
    return {"ok": True}
