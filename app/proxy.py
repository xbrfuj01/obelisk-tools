"""Shared helpers for talking to the optional module sidecars (Scroll
Recorder today; Downloader+Converter and Metadata once they're split out
the same way - see modules.py for how a module's availability is tracked).

Each module lives in its own container, unauthenticated internally and
unreachable from outside the compose network - these helpers are the only
place that talks to them, so every route using them inherits the site's own
session auth/rate-limiting instead of the module needing its own."""

import httpx
from fastapi.responses import JSONResponse, StreamingResponse


async def proxy_json(base_url: str, method: str, path: str, json_body=None, timeout=10,
                      unavailable_message="Модуль недоступний"):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{base_url}{path}", json=json_body)
    except httpx.HTTPError:
        return JSONResponse({"error": unavailable_message}, status_code=502)
    return JSONResponse(resp.json(), status_code=resp.status_code)


async def proxy_file_stream(base_url: str, path: str, filename: str, media_type: str,
                             unavailable_message="Модуль недоступний",
                             not_ready_message="Файл ще не готовий"):
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream_req = client.build_request("GET", f"{base_url}{path}")
        resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse({"error": unavailable_message}, status_code=502)
    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        return JSONResponse({"error": not_ready_message}, status_code=404)

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
