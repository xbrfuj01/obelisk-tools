import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import auth, config
from .database import SessionLocal
from .downloader import parse_timecode
from .models import Conversion

_executor = ThreadPoolExecutor(max_workers=16)

QUALITY_PRESETS = {
    "high": {"crf": 18, "preset": "slow"},
    "medium": {"crf": 23, "preset": "medium"},
    "low": {"crf": 28, "preset": "fast"},
}

PROGRESS_KEYS = {
    "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
    "out_time", "dup_frames", "drop_frames", "speed", "progress",
}

# Same idea as downloader.py's constant of the same name - see there for why.
ETA_SMOOTHING_ALPHA = 0.25


class _ConcurrencyGate:
    """Caps how many conversions actually run at once, against a limit that
    can change at runtime (read fresh from the DB on every acquire). Separate
    from the downloader's gate — encoding is CPU-bound, downloading is
    mostly I/O-bound, so mixing them into one limit would let one starve
    the other."""

    def __init__(self):
        self._cv = threading.Condition()
        self._active = 0

    def acquire(self, limit: int):
        with self._cv:
            while self._active >= limit:
                self._cv.wait()
            self._active += 1

    def release(self):
        with self._cv:
            self._active -= 1
            self._cv.notify()


_gate = _ConcurrencyGate()

# Same idea as downloader.py's: a queued job checks this once it gets its
# concurrency slot, and a running one is caught inside _run_ffmpeg's line
# loop (which, reading ffmpeg's own -progress stream, gets a chance to
# check several times a second).
_cancel_requested = set()


def request_cancel(job_id: str):
    _cancel_requested.add(job_id)


def _parse_fps(rate: str):
    try:
        if "/" in rate:
            num, den = rate.split("/")
            den = float(den)
            return float(num) / den if den else None
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return None


def _run_ffprobe(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _int_or_none(value):
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _estimate_video_bitrate(fmt, video, audio, filesize, duration):
    """Bits/sec for the source video stream, used to match the output's
    bitrate to the original instead of targeting a fixed quality level.
    Falls back progressively: the video stream's own bit_rate, then the
    container's overall bitrate minus the audio track's, then a plain
    filesize/duration estimate."""
    direct = _int_or_none(video.get("bit_rate"))
    if direct:
        return direct

    total = _int_or_none(fmt.get("bit_rate"))
    if total:
        audio_bitrate = _int_or_none(audio.get("bit_rate")) if audio else 0
        estimate = total - (audio_bitrate or 0)
        if estimate > 0:
            return estimate

    if filesize and duration:
        return int(filesize * 8 / duration)

    return None


def probe_input(path):
    """Reads container/codec/resolution info via ffprobe. Returns None if
    ffprobe can't read the file or it has no video stream at all."""
    data = _run_ffprobe(path)
    if not data:
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        return None

    duration = None
    for candidate in (fmt.get("duration"), video.get("duration")):
        try:
            if candidate:
                duration = float(candidate)
                break
        except (TypeError, ValueError):
            continue

    fps = _parse_fps(video.get("r_frame_rate") or "") or 30.0
    width = video.get("width")
    height = video.get("height")
    container = (fmt.get("format_name") or "").split(",")[0].upper()
    vcodec = (video.get("codec_name") or "").upper()
    acodec = (audio.get("codec_name") or "").upper() if audio else None

    parts = [container or "?"]
    if width and height:
        parts.append(f"{width}×{height}")
    if vcodec:
        parts.append(vcodec)
    if fps:
        fps_label = f"{fps:.2f}".rstrip("0").rstrip(".")
        parts.append(f"{fps_label} fps")
    parts.append(f"аудіо {acodec}" if acodec else "без звуку")
    if duration:
        m, s = divmod(int(duration), 60)
        h, m = divmod(m, 60)
        parts.append(f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")

    filesize = None
    try:
        filesize = os.path.getsize(path)
    except OSError:
        pass
    video_bitrate = _estimate_video_bitrate(fmt, video, audio, filesize, duration)

    return {
        "summary": " · ".join(parts),
        "duration": duration,
        "fps": fps,
        "has_audio": audio is not None,
        "video_bitrate": video_bitrate,
        "vcodec": vcodec,
        "acodec": acodec,
    }


def is_premiere_compatible(vcodec, acodec):
    """H.264 video (+ AAC audio, if there's audio at all) is what Premiere
    can decode directly - anything else (VP9/AV1 + Opus is YouTube's usual
    default for "best quality") needs a re-encode first."""
    return vcodec == "H264" and (acodec is None or acodec == "AAC")


def submit_conversion_from_download(download_job):
    """Auto-triggered right after a download finishes when premiere_compat
    is on and the file's actual codec turned out unsuitable - copies it
    into a new conversion job (same shape as the manual "Конвертувати для
    Premiere" flow) and starts it immediately. Returns the new Conversion
    job's id, or None if the file can't be read at all."""
    info = probe_input(download_job.filepath)
    if not info:
        return None

    db = SessionLocal()
    try:
        job_id = uuid.uuid4().hex
        job_dir = os.path.join(config.DOWNLOAD_DIR, "converts", job_id)
        os.makedirs(job_dir, exist_ok=True)

        original_name = os.path.basename(download_job.filepath)
        ext = os.path.splitext(original_name)[1][:10] or ".bin"
        input_path = os.path.join(job_dir, f"input{ext}")
        shutil.copyfile(download_job.filepath, input_path)

        job = Conversion(
            id=job_id,
            original_filename=original_name,
            input_summary=info["summary"],
            duration_seconds=info["duration"],
            quality="high",
            audio_option="original",
            is_auto=True,
            status="queued",
            client_ip=download_job.client_ip,
            client_id=download_job.client_id,
            username=download_job.username,
        )
        db.add(job)
        db.commit()
    finally:
        db.close()

    submit_job(job_id, input_path, info)
    return job_id


def _build_ffmpeg_cmd(input_path, output_path, quality, audio_option, fps, has_audio, video_bitrate=None):
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["medium"])
    # A keyframe roughly every second (rather than the long GOPs typical of
    # camera/phone/web footage) is what actually makes H.264 smooth to
    # scrub and multicam-sync in Premiere - as much a part of "Premiere
    # compatibility" as the codec choice itself.
    gop = max(1, round(fps)) if fps else 30

    cmd = ["ffmpeg", "-y", "-i", input_path, "-map", "0:v:0", "-c:v", "libx264"]

    if quality == "high" and video_bitrate:
        # "Оригінальна якість" means matching the source's own bitrate, not
        # re-encoding at a fixed high CRF - CRF targets a quality level, so
        # feeding it an already-compressed source just re-encodes existing
        # compression artifacts at huge expense in size for no real quality
        # gain. Capped VBR around the source bitrate keeps the output in
        # the same size ballpark; only the container/profile/GOP change.
        kbps = max(200, video_bitrate // 1000)
        cmd += [
            "-preset", "slow",
            "-b:v", f"{kbps}k",
            "-maxrate", f"{int(kbps * 1.5)}k",
            "-bufsize", f"{kbps * 2}k",
        ]
    else:
        cmd += ["-preset", preset["preset"], "-crf", str(preset["crf"])]

    cmd += [
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.2",
        "-g", str(gop),
        "-keyint_min", str(gop),
    ]

    if audio_option == "none" or not has_audio:
        cmd.append("-an")
    else:
        cmd += ["-map", "0:a:0"]
        if audio_option == "original":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]

    cmd += ["-movflags", "+faststart", "-loglevel", "error", "-progress", "pipe:1", "-nostats", output_path]
    return cmd


# A hard ceiling on a single encode, regardless of file size. Without this a
# hung ffmpeg process (a malformed/adversarial input can make some codecs
# wait forever) would sit in its concurrency-gate slot permanently - with
# the default limit of 1 concurrent conversion, that's a full outage of the
# converter feature until the container restarts.
FFMPEG_TIMEOUT_SECONDS = 3 * 3600


def _run_ffmpeg(cmd, job_id, duration):
    db = SessionLocal()
    log_tail = []
    last_commit = 0.0
    last_speed = None
    timed_out = False
    proc = None
    watchdog = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        def _kill_on_timeout():
            nonlocal timed_out
            timed_out = True
            proc.kill()

        watchdog = threading.Timer(FFMPEG_TIMEOUT_SECONDS, _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()

        cancelled = False
        for raw_line in proc.stdout:
            if job_id in _cancel_requested:
                cancelled = True
                proc.kill()
                break
            line = raw_line.strip()
            if not line:
                continue
            key, sep, value = line.partition("=")
            if sep and key in PROGRESS_KEYS:
                # ffmpeg reports how many seconds of output it's producing per
                # wall-clock second (e.g. "2.53x") - combined with how much
                # is left, that gives a real ETA instead of a guess.
                if key == "speed":
                    try:
                        raw_speed = float(value.strip().rstrip("x"))
                        if raw_speed > 0:
                            # Momentary dips (a hard-to-encode scene, a
                            # slow keyframe) make the raw value jump around
                            # tick to tick - an exponential moving average
                            # nudges the estimate toward each new sample
                            # instead of replacing it outright, so ETA
                            # reacts to a real speed change without
                            # visibly jittering.
                            last_speed = raw_speed if not last_speed else (
                                ETA_SMOOTHING_ALPHA * raw_speed + (1 - ETA_SMOOTHING_ALPHA) * last_speed
                            )
                    except ValueError:
                        pass
                # ffmpeg's -progress can report multiple times a second (up
                # to once per frame) - throttle DB writes to a couple times a
                # second instead of hammering SQLite on every line.
                now = time.time()
                if key == "out_time" and duration and now - last_commit >= 0.5:
                    seconds = parse_timecode(value)
                    if seconds is not None:
                        job = db.get(Conversion, job_id)
                        if job:
                            job.progress = round(min(99.0, seconds / duration * 100), 1)
                            job.status = "converting"
                            if last_speed:
                                job.eta_seconds = max(0, round((duration - seconds) / last_speed))
                            db.commit()
                    last_commit = now
            else:
                log_tail.append(line)
                del log_tail[:-20]
        proc.wait()
        if cancelled:
            return "cancelled", ""
        if timed_out:
            log_tail.append(f"ffmpeg перевищив ліміт часу ({FFMPEG_TIMEOUT_SECONDS // 3600} год) і був примусово зупинений")
            return 1, "\n".join(log_tail)
        return proc.returncode, "\n".join(log_tail)
    finally:
        if watchdog:
            watchdog.cancel()
        db.close()


def _run_job(job_id: str, input_path: str, info: dict):
    db = SessionLocal()
    job = db.get(Conversion, job_id)
    if not job:
        db.close()
        return

    limit = auth.get_max_concurrent_conversions(db)
    _gate.acquire(limit)
    try:
        if job_id in _cancel_requested:
            # Cancelled while it was still waiting for a concurrency slot -
            # never actually started, so there's nothing to abort mid-flight.
            job.status = "cancelled"
            job.eta_seconds = None
            job.finished_at = datetime.utcnow()
            db.commit()
            shutil.rmtree(os.path.dirname(input_path), ignore_errors=True)
            return

        job.status = "converting"
        db.commit()

        job_dir = os.path.dirname(input_path)
        base = os.path.splitext(job.original_filename or "video")[0]
        base = re.sub(r"[^\w\-. ]", "_", base).strip(" .") or "video"
        output_path = os.path.join(job_dir, f"{base}_premiere.mp4")

        has_audio = info.get("has_audio", True)
        fps = info.get("fps")
        duration = info.get("duration")
        video_bitrate = info.get("video_bitrate")

        cmd = _build_ffmpeg_cmd(input_path, output_path, job.quality, job.audio_option, fps, has_audio, video_bitrate)
        returncode, log_tail = _run_ffmpeg(cmd, job_id, duration)

        if returncode != 0 and returncode != "cancelled" and job.audio_option == "original":
            # Stream-copying the source audio into an MP4 container can fail
            # outright for codecs MP4 can't hold (e.g. Vorbis/Opus from a
            # webm source) - fall back to a real AAC re-encode once instead
            # of just surfacing an error for something we can recover from.
            cmd = _build_ffmpeg_cmd(input_path, output_path, job.quality, "aac", fps, has_audio, video_bitrate)
            returncode, log_tail = _run_ffmpeg(cmd, job_id, duration)

        if returncode == "cancelled":
            job.status = "cancelled"
            job.eta_seconds = None
            job.finished_at = datetime.utcnow()
            db.commit()
            shutil.rmtree(job_dir, ignore_errors=True)
            return

        if returncode != 0 or not os.path.exists(output_path):
            raise RuntimeError(log_tail[-500:] if log_tail else "Помилка конвертації ffmpeg")

        job.status = "finished"
        job.progress = 100.0
        job.eta_seconds = None
        job.filepath = output_path
        job.filesize = os.path.getsize(output_path)
        job.finished_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        job.status = "error"
        job.eta_seconds = None
        job.error_message = str(e)[:500]
        job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except OSError:
                pass
        _cancel_requested.discard(job_id)
        _gate.release()
        db.close()


def submit_job(job_id: str, input_path: str, info: dict):
    _executor.submit(_run_job, job_id, input_path, info)
