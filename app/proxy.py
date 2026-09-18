"""Shared helpers for talking to the optional module sidecars (Scroll
Recorder, Downloader+Converter, Metadata - see modules.py for how a
module's availability is tracked).

Each module lives in its own container, unauthenticated internally and
unreachable from outside the compose network - these helpers are the only
place that talks to them, so every route using them inherits the site's own
session auth/rate-limiting instead of the module needing its own."""

import json
import os
import uuid

import httpx
from fastapi.responses import JSONResponse, StreamingResponse


def _normalize_error_payload(resp):
    try:
        payload = resp.json()
    except ValueError:
        return {"error": resp.text[:300]}
    # Module routes mostly raise a plain FastAPI HTTPException, which
    # serializes as {"detail": ...} - the existing frontend JS across every
    # tool only ever reads .error, a holdover from when these routes lived
    # in core and returned that shape by hand. Normalizing here means every
    # proxied route gets this for free instead of each module route having
    # to remember to match it.
    if resp.status_code >= 400 and isinstance(payload, dict) and "error" not in payload and "detail" in payload:
        payload["error"] = payload["detail"]
    return payload


async def fetch_json(base_url: str, path: str, params=None, timeout=5, default=None):
    """Best-effort GET for server-side template pre-rendering (e.g. a
    page's initial "recent items" list) - swallows any failure and returns
    `default` instead, since the page's own JS re-fetches this right after
    anyway and a down/slow module should degrade to an empty list, not a
    500 on the page itself."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}{path}", params=params)
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError):
        pass
    return default


async def proxy_json(base_url: str, method: str, path: str, json_body=None, params=None, timeout=10,
                      unavailable_message="Модуль недоступний"):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{base_url}{path}", json=json_body, params=params)
    except httpx.HTTPError:
        return JSONResponse({"error": unavailable_message}, status_code=502)
    return JSONResponse(_normalize_error_payload(resp), status_code=resp.status_code)


async def proxy_file_stream(base_url: str, path: str, timeout=None,
                             unavailable_message="Модуль недоступний"):
    """Streams a GET response straight through, relaying the upstream's own
    Content-Type/Content-Disposition/status/error body untouched - the
    module's own route already sets these correctly (FileResponse or an
    HTTPException), so there's nothing module-specific for the proxy layer
    to know or hardcode."""
    client = httpx.AsyncClient(timeout=timeout)
    try:
        upstream_req = client.build_request("GET", f"{base_url}{path}")
        resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse({"error": unavailable_message}, status_code=502)
    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "Файл недоступний"}, status_code=resp.status_code)
        if resp.status_code >= 400 and isinstance(payload, dict) and "error" not in payload and "detail" in payload:
            payload["error"] = payload["detail"]
        return JSONResponse(payload, status_code=resp.status_code)

    media_type = resp.headers.get("content-type", "application/octet-stream")
    headers = {}
    content_disposition = resp.headers.get("content-disposition")
    if content_disposition:
        headers["Content-Disposition"] = content_disposition

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), media_type=media_type, headers=headers)


async def proxy_upload(base_url: str, path: str, upload_file, max_bytes: int, scratch_dir: str,
                        extra_fields=None, too_large_message="Файл завеликий", timeout=120,
                        unavailable_message="Модуль недоступний"):
    """Buffers an incoming multipart upload to a scratch file on core's own
    disk (enforcing max_bytes while doing it, the same size-limit check the
    monolith used to do inline) then re-uploads that file to the module -
    streamed from disk, not held in memory, same as the file the module
    itself will write to its own disk right after. extra_fields (plain
    strings) are sent alongside it as regular multipart form fields, e.g.
    the client_id a job row needs to record ownership."""
    os.makedirs(scratch_dir, exist_ok=True)
    tmp_path = os.path.join(scratch_dir, uuid.uuid4().hex)
    total = 0
    too_large = False
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await upload_file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    too_large = True
                    break
                out.write(chunk)
        if too_large:
            return JSONResponse({"error": too_large_message}, status_code=413)

        try:
            with open(tmp_path, "rb") as f:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{base_url}{path}",
                        files={"file": (upload_file.filename or "file", f, upload_file.content_type)},
                        data=extra_fields or {},
                    )
        except httpx.HTTPError:
            return JSONResponse({"error": unavailable_message}, status_code=502)
        return JSONResponse(_normalize_error_payload(resp), status_code=resp.status_code)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
