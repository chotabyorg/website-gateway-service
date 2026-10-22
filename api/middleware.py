import logging

from aiohttp import web

from core.logger import log_event

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Widget-Key, X-Widget-Session-Token",
    "Access-Control-Max-Age": "3600",
}


def apply_response_headers(request: web.Request, response: web.StreamResponse) -> None:
    # The managed handler explicitly prepares/writes; these must precede prepare.
    response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS["Access-Control-Allow-Headers"]


@web.middleware
async def safe_error_middleware(request: web.Request, handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:
        log_event(logging.ERROR, "gateway_request_failed", exc=exc)
        response = web.json_response({"error": "Service unavailable"}, status=500)
        apply_response_headers(request, response)
        return response


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.StreamResponse:
    origin = request.headers.get("Origin", "*")
    if request.method == "OPTIONS":
        return web.Response(status=200, headers={"Access-Control-Allow-Origin": origin, **_CORS_HEADERS})
    response = await handler(request)
    if not response.prepared:
        apply_response_headers(request, response)
    return response
