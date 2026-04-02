from aiohttp import web

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Widget-Key",
    "Access-Control-Max-Age": "3600",
}


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.Response:
    origin = request.headers.get("Origin", "*")

    if request.method == "OPTIONS":
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": origin,
                **_CORS_HEADERS,
            },
        )

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS["Access-Control-Allow-Headers"]
    return response
