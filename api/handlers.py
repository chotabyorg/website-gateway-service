import json

from aiohttp import web

from core.logger import logger


async def handle_chat(request: web.Request) -> web.Response:
    api_key = request.headers.get("X-Widget-Key")
    if not api_key:
        return web.json_response({"error": "Missing X-Widget-Key header"}, status=401)

    key_manager = request.app["key_manager"]
    widget_info = key_manager.validate_key(api_key)
    if widget_info is None:
        return web.json_response({"error": "Invalid widget key"}, status=401)

    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    session_id = body.get("session_id")
    message = body.get("message")

    if not session_id:
        return web.json_response({"error": "Missing session_id"}, status=400)
    if not message:
        return web.json_response({"error": "Missing message"}, status=400)

    core_api = request.app["core_api"]
    logger.info(
        "Chat request: widget=%s agent=%s session=%s",
        widget_info.integration_id,
        widget_info.agent_id,
        session_id,
    )
    reply = await core_api.send_message(widget_info.agent_id, session_id, message)

    return web.json_response({"reply": reply, "session_id": session_id})


async def handle_config(request: web.Request) -> web.Response:
    key = request.rel_url.query.get("key")
    if not key:
        return web.json_response({"error": "Missing key parameter"}, status=400)

    key_manager = request.app["key_manager"]
    widget_info = key_manager.validate_key(key)
    if widget_info is None:
        return web.json_response({"error": "Invalid widget key"}, status=404)

    return web.json_response({
        "primary_color": widget_info.config.get("primary_color", "#6C5CE7"),
        "position": widget_info.config.get("position", "bottom-right"),
        "welcome_message": widget_info.config.get("welcome_message", "Привет! Чем могу помочь?"),
        "window_title": widget_info.config.get("window_title", "Чат с ассистентом"),
    })


async def handle_health(request: web.Request) -> web.Response:
    key_manager = request.app["key_manager"]
    return web.json_response({"status": "ok", "active_widgets": key_manager.active_count()})


async def handle_root(request: web.Request) -> web.Response:
    return web.json_response({"service": "website-gateway-service", "status": "running"})
