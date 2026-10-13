import asyncio
import json
import logging

import httpx
from aiohttp import web

from core.logger import log_event
from core.routing import RoutingPolicy, strict_uuid
from api.middleware import apply_response_headers
from services.conversation_client import (
    ConversationConflict,
    ConversationUnavailable,
    WebsiteEnvelope,
    response_attempt_for,
)
from services.text_formatter import format_plain_text_reply


async def handle_chat(request: web.Request) -> web.Response:
    api_key = request.headers.get("X-Widget-Key")
    if not api_key:
        return web.json_response({"error": "Missing X-Widget-Key header"}, status=401)

    key_manager = request.app["key_manager"]
    try:
        widget_info = await key_manager.validate_key_for_chat(api_key)
    except httpx.HTTPError:
        return web.json_response({"error": "Widget validation unavailable"}, status=503)
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

    log_event(logging.INFO, "chat_request", integration_id=widget_info.integration_id,
              agent_id=widget_info.agent_id, session_id=session_id)
    policy = request.app.get("routing_policy", RoutingPolicy.parse())
    guarded_agent = policy.guards.get(widget_info.integration_id)
    selected = policy.selected(widget_info.integration_id, session_id)
    if guarded_agent is not None:
        if widget_info.agent_id != guarded_agent:
            return web.json_response({"error": "Managed integration binding unavailable"}, status=503)
        message_id = body.get("message_id")
        if selected or "message_id" in body:
            try:
                envelope = WebsiteEnvelope(
                    owner_id=strict_uuid(widget_info.owner_id),
                    integration_id=strict_uuid(widget_info.integration_id),
                    agent_id=strict_uuid(widget_info.agent_id),
                    session_id=strict_uuid(session_id), message_id=strict_uuid(message_id), text=message,
                )
                if not isinstance(message, str):
                    raise ValueError("invalid_managed_message")
            except ValueError:
                return web.json_response({"error": "Managed requests require UUID session_id and message_id and text message"}, status=400)
            conversation = request.app.get("conversation_client")
            if conversation is None:
                return web.json_response({"error": "Managed service unavailable"}, status=503)
            try:
                disposition = await conversation.admit(envelope, "MANAGED" if selected else "LEGACY")
                if disposition == "MANAGED":
                    return await _managed_reply(request, conversation, envelope)
                if disposition != "LEGACY":
                    raise ConversationUnavailable("invalid_admission_disposition")
            except ConversationConflict:
                return web.json_response({"error": "Message identity conflict"}, status=409)
            except ConversationUnavailable:
                return web.json_response({"error": "Managed service unavailable; retry the same message_id"}, status=503)

    reply = await request.app["core_api"].send_message(
        widget_info.agent_id, session_id, message, owner_id=widget_info.owner_id,
    )
    return web.json_response({"reply": reply, "session_id": session_id})


async def _complete_transport(conversation, envelope, attempt: str, outcome: str, timeout: float = 10) -> None:
    # Bound, shield and join the owned task even if the request is cancelled.
    task = asyncio.create_task(asyncio.wait_for(
        conversation.complete_response(envelope, attempt, outcome), timeout=timeout))
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                break
            cancelled = True
        except Exception as exc:
            log_event(logging.ERROR, "managed_transport_reconciliation_failed", exc=exc)
            break
    if cancelled:
        raise asyncio.CancelledError


async def _managed_reply(request: web.Request, conversation, envelope: WebsiteEnvelope) -> web.Response:
    result = await conversation.wait_result(envelope)
    if result["state"] != "DRAFT_READY":
        error = "Managed result pending; retry the same message_id"
        if result["state"] == "UNKNOWN":
            error = "Managed execution uncertain; retry the same message_id"
        elif result["state"] in ("FAILED", "NO_RESPONSE"):
            error = "Managed result unavailable; retry the same message_id"
        return web.json_response({"error": error}, status=503)

    # A retry of the same canonical request keeps the same transport identity,
    # including when this Gateway process restarted after a socket write.
    attempt = response_attempt_for(envelope.message_id)
    intent = await conversation.begin_response(envelope, attempt)
    if not intent.send:
        # A retained draft is content, not permission. Superseded output is a
        # conflict; unresolved delivery evidence asks the client to retry later.
        superseded = intent.denial_reason == "SUPERSEDED"
        response = web.json_response(
            {"error": ("response_superseded" if superseded else "response_unavailable"),
             "session_id": envelope.session_id}, status=409 if superseded else 503
        )
        response.headers["Cache-Control"] = "no-store"
        apply_response_headers(request, response)
        return response
    response = web.json_response({"reply": format_plain_text_reply(intent.draft_text), "session_id": envelope.session_id})
    response.headers["Cache-Control"] = "no-store"
    apply_response_headers(request, response)
    outcome = "UNKNOWN"
    try:
        await response.prepare(request)
        await response.write_eof()
        outcome = "WRITE_COMPLETED"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome = "WRITE_FAILED"
        log_event(logging.WARNING, "managed_response_write_failed", exc=exc)
    finally:
        await _complete_transport(conversation, envelope, attempt, outcome, request.app.get("transport_timeout", 10))
    return response


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
