"""Credential-bearing Website ingress and history; Conversation owns all state."""
from __future__ import annotations

import json
import re
from typing import Annotated
from uuid import UUID

import httpx
from aiohttp import web
from pydantic import BeforeValidator, SecretStr

from core.routing import strict_uuid
from services.website_session_client import WebsiteSessionError
from services.website_session_contracts import (
    Contract, TextContent, WebsiteHistoryRequest, WebsiteSessionAdmission,
    WebsiteSessionCreate, WebsiteSessionScope,
)

PublicUUID = Annotated[UUID, BeforeValidator(strict_uuid)]


class Create(Contract):
    pass


class Message(Contract):
    session_id: PublicUUID
    message_id: PublicUUID
    message: TextContent


class History(Contract):
    session_id: PublicUUID
    cursor: PublicUUID | None = None


def response(payload, status=200, maximum=4096, retry_after=None):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > maximum:
        encoded, status = b'{"error":"Session unavailable"}', 503
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, min(60, retry_after)))
    return web.Response(body=encoded, status=status, content_type="application/json", headers=headers)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result


async def read_body(request, model):
    if request.content_type != "application/json":
        raise ValueError("invalid_body")
    data = bytearray()
    async for chunk in request.content.iter_chunked(8192):
        if len(data) + len(chunk) > 65536:
            raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=65537)
        data.extend(chunk)
    return model.model_validate(json.loads(data.decode("utf-8"), object_pairs_hook=unique_object))


async def handle(request, operation):
    policy = request.app.get("public_session_policy")
    client = request.app.get("website_session_client")
    if policy is None or not policy.integrations or client is None:
        return response({"error": "Not found"}, 404)
    try:
        client_ip = policy.client_ip(request)
    except (ValueError, TypeError):
        return response({"error": "HTTPS required"}, 400)
    keys = request.headers.getall("X-Widget-Key", [])
    if len(keys) != 1 or not 1 <= len(keys[0]) <= 256:
        return response({"error": "Invalid widget key"}, 401)
    try:
        body = await read_body(request, {"create": Create, "message": Message,
                                         "history": History}[operation])
    except web.HTTPRequestEntityTooLarge:
        return response({"error": "Request too large"}, 413)
    except (ValueError, TypeError):
        return response({"error": "Invalid session request"}, 400)
    try:
        widget = await request.app["key_manager"].validate_key_for_chat(keys[0])
    except httpx.HTTPError:
        return response({"error": "Widget validation unavailable"}, 503)
    if widget is None:
        return response({"error": "Invalid widget key"}, 401)
    if (policy.integrations.get(widget.integration_id) != widget.agent_id
            or widget.conversation_required is not True):
        return response({"error": "Session unavailable"}, 503)
    try:
        scope = WebsiteSessionScope(owner_id=widget.owner_id, integration_id=widget.integration_id,
            agent_id=widget.agent_id, binding_generation=widget.binding_generation)
        if operation == "create":
            created = await client.create(WebsiteSessionCreate(**scope.model_dump(), client_ip=client_ip))
            return response(created.model_dump(mode="json"), 201)
        tokens = request.headers.getall("X-Widget-Session-Token", [])
        if len(tokens) != 1 or re.fullmatch(r"[A-Za-z0-9_-]{43}", tokens[0]) is None:
            return response({"error": "Session unavailable"}, 404)
        access = dict(scope.model_dump(), session_id=body.session_id,
                      session_token=SecretStr(tokens[0]))
        if operation == "message":
            await client.admit(WebsiteSessionAdmission(**access, message_id=body.message_id,
                                                       text=body.message))
            return response({"message_id": str(body.message_id), "status": "ACCEPTED"}, 202)
        page = await client.history(WebsiteHistoryRequest(**access, cursor=body.cursor))
        return response(page.model_dump(mode="json"), maximum=262144)
    except WebsiteSessionError as error:
        return response({"error": "Session unavailable"}, error.status, retry_after=error.retry_after)
    except (ValueError, TypeError):
        return response({"error": "Session unavailable"}, 503)


async def create(request):
    return await handle(request, "create")


async def message(request):
    return await handle(request, "message")


async def history(request):
    return await handle(request, "history")
