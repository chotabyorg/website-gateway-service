"""Public HTTPS boundary with a real HTTP server and closed private fake peer."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

from api import website_sessions
from api.middleware import cors_middleware, safe_error_middleware
from core.website_public_policy import WebsitePublicPolicy
from services.control_plane_client import WidgetValidation
from services.website_session_client import WebsiteSessionClient
from services.widget_key_manager import WidgetKeyManager

INTEGRATION, AGENT, SESSION = (str(uuid4()) for _ in range(3))
TOKEN = "a" * 43
HEADERS = {"X-Widget-Key": "wk_fixture", "X-Forwarded-Proto": "https",
           "X-Real-IP": "192.0.2.1", "X-Widget-Session-Token": TOKEN}


@pytest_asyncio.fixture
async def public():
    calls, state = [], {"status": 200, "items": []}

    def respond(request):
        calls.append((request, json.loads(request.content)))
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"secret": "private-error"},
                                  headers={"Retry-After": "12"})
        if request.url.path.endswith("/admissions"):
            payload = {"route": "MANAGED", "admission_id": str(uuid4()), "replayed": False}
        elif request.url.path.endswith("/history"):
            payload = {"state": "PAGE", "items": state["items"], "next_cursor": None,
                       "oversized_message_id": None}
        else:
            return httpx.Response(201, json={"session_id": SESSION, "session_token": TOKEN,
                                            "expires_at": "2026-10-23T00:00:00Z"})
        return httpx.Response(200, json=payload)

    private = WebsiteSessionClient("https://conversation.internal.example", "private-only",
        expected_peer="conversation.internal.example", transport=httpx.MockTransport(respond))
    cp = AsyncMock()
    cp.validate_widget_key.return_value = WidgetValidation(INTEGRATION, AGENT, "trusted-owner", {},
                                                          True, 7)
    app = web.Application(middlewares=[safe_error_middleware, cors_middleware])
    app["public_session_policy"] = WebsitePublicPolicy.parse(
        json.dumps({INTEGRATION: AGENT}), "127.0.0.1/32")
    app["website_session_client"] = private
    app["key_manager"] = WidgetKeyManager(cp)
    app["core_api"] = AsyncMock()
    app.router.add_post("/v1/sessions", website_sessions.create)
    app.router.add_post("/v1/sessions/messages", website_sessions.message)
    app.router.add_post("/v1/sessions/history", website_sessions.history)
    try:
        async with TestClient(TestServer(app)) as client:
            yield SimpleNamespace(client=client, calls=calls, state=state, cp=cp, app=app)
        assert not app["core_api"].mock_calls
    finally:
        await private.close()


@pytest.mark.asyncio
async def test_create_message_history_use_server_identity_and_private_json(public):
    created = await public.client.post("/v1/sessions", json={}, headers=HEADERS)
    assert created.status == 201
    assert (await created.json())["session_token"] == TOKEN
    assert created.headers["Cache-Control"] == "no-store"
    message_id = str(uuid4())
    for _ in range(2):
        admitted = await public.client.post("/v1/sessions/messages", headers=HEADERS,
            json={"session_id": SESSION, "message_id": message_id, "message": "exact\nfixture"})
        assert admitted.status == 202
        assert await admitted.json() == {"message_id": message_id, "status": "ACCEPTED"}
    public.state["items"] = [{"id": str(uuid4()), "actor": "HUMAN", "text": "owner fixture",
                              "client_message_id": None}]
    page = await public.client.post("/v1/sessions/history", headers=HEADERS,
                                    json={"session_id": SESSION})
    assert page.status == 200 and (await page.json())["items"] == public.state["items"]
    assert page.headers["Cache-Control"] == "no-store"
    assert "X-Widget-Session-Token" in page.headers["Access-Control-Allow-Headers"]
    assert public.cp.validate_widget_key.await_count == 4
    assert public.calls[0][1] == {"owner_id": "trusted-owner", "integration_id": INTEGRATION,
        "agent_id": AGENT, "binding_generation": 7, "client_ip": "192.0.2.1"}
    for request, payload in public.calls:
        assert request.url.scheme == "https" and request.url.query == b""
        assert TOKEN not in str(request.url) and TOKEN not in str(request.headers)
        assert payload["owner_id"] == "trusted-owner" and payload["binding_generation"] == 7
    assert public.calls[1][1]["message_id"] == public.calls[2][1]["message_id"] == message_id
    assert public.calls[-1][1]["session_token"] == TOKEN


@pytest.mark.parametrize("path,payload", [
    ("", {"session_id": SESSION}),
    ("", {"owner_id": "forged"}),
    ("/messages", {"session_id": SESSION, "message_id": str(uuid4()), "message": "fixture",
                    "binding_generation": 1}),
    ("/messages", {"session_id": "opaque", "message_id": str(uuid4()), "message": "fixture"}),
    ("/messages", {"session_id": SESSION, "message_id": str(uuid4()), "message": []}),
    ("/history", {"session_id": SESSION, "cursor": "opaque"}),
    ("/history", {"session_id": SESSION, "limit": 1000}),
])
@pytest.mark.asyncio
async def test_public_fields_are_closed_before_private_io(public, path, payload):
    result = await public.client.post("/v1/sessions" + path, json=payload, headers=HEADERS)
    assert result.status == 400
    assert public.calls == [] and public.cp.validate_widget_key.await_count == 0


@pytest.mark.parametrize("extra", [
    {}, {"X-Forwarded-Proto": "http"},
    {"X-Forwarded-Proto": "https,http", "X-Real-IP": "192.0.2.1"},
    {"X-Forwarded-Proto": "https", "X-Real-IP": "192.0.2.1,192.0.2.2"},
])
@pytest.mark.asyncio
async def test_missing_or_ambiguous_tls_proof_has_zero_credentials_io(public, extra):
    result = await public.client.post("/v1/sessions", json={},
        headers={"X-Widget-Key": "wk_fixture", **extra})
    assert result.status == 400
    assert public.calls == [] and public.cp.validate_widget_key.await_count == 0


@pytest.mark.parametrize("name", ["X-Forwarded-Proto", "X-Real-IP", "X-Widget-Key"])
@pytest.mark.asyncio
async def test_duplicate_trust_headers_rejected(public, name):
    headers = CIMultiDict(HEADERS)
    headers.add(name, HEADERS[name])
    result = await public.client.post("/v1/sessions", json={}, headers=headers)
    assert result.status in {400, 401}
    assert public.calls == [] and public.cp.validate_widget_key.await_count == 0


@pytest.mark.asyncio
async def test_untrusted_http_forwarding_cannot_assert_https(public):
    public.app["public_session_policy"] = WebsitePublicPolicy.parse(
        json.dumps({INTEGRATION: AGENT}), "10.0.0.1/32")
    result = await public.client.post("/v1/sessions", json={}, headers=HEADERS)
    assert result.status == 400
    assert not public.calls and not public.cp.mock_calls


@pytest.mark.parametrize("body,status", [
    (b'{"session_id":"x","session_id":"y"}', 400),
    (b'{"unknown":"' + b'a' * 65536 + b'"}', 413),
    (b'[]', 400), (b'null', 400), (b'\xff', 400),
], ids=["duplicate", "oversize", "array", "null", "non-utf8"])
@pytest.mark.asyncio
async def test_bounded_public_json_before_private_io(public, body, status):
    result = await public.client.post("/v1/sessions", data=body,
        headers={**HEADERS, "Content-Type": "application/json"})
    assert result.status == status
    assert not public.calls and not public.cp.mock_calls


@pytest.mark.parametrize("token", ["", "bad", "a" * 44])
@pytest.mark.asyncio
async def test_missing_or_malformed_capability_cannot_read(public, token):
    result = await public.client.post("/v1/sessions/history", json={"session_id": SESSION},
                                      headers={**HEADERS, "X-Widget-Session-Token": token})
    assert result.status == 404 and public.calls == []


@pytest.mark.parametrize("status", [404, 409, 413, 429, 500])
@pytest.mark.asyncio
async def test_private_failures_are_bounded_and_preserve_rate_limit(public, status):
    public.state["status"] = status
    result = await public.client.post("/v1/sessions", json={}, headers=HEADERS)
    assert result.status == (503 if status == 500 else status)
    assert await result.json() == {"error": "Session unavailable"}
    assert result.headers["Cache-Control"] == "no-store"
    if status == 429:
        assert result.headers["Retry-After"] == "12"
    assert len(public.calls) == 1


@pytest.mark.asyncio
async def test_disabled_cohort_precedes_tls_body_and_key_processing(public):
    public.app["public_session_policy"] = WebsitePublicPolicy.parse()
    result = await public.client.post("/v1/sessions", data=b'not-json')
    assert result.status == 404 and not public.calls and not public.cp.mock_calls


@pytest.mark.parametrize("required,agent", [(False, AGENT), (True, str(uuid4()))])
@pytest.mark.asyncio
async def test_fresh_private_binding_cannot_be_overridden(public, required, agent):
    public.cp.validate_widget_key.return_value = WidgetValidation(INTEGRATION, agent,
        "trusted-owner", {}, required, 7)
    result = await public.client.post("/v1/sessions", json={}, headers=HEADERS)
    assert result.status == 503 and not public.calls


def test_direct_tls_uses_actual_peer_and_ignores_untrusted_forwarding():
    request = SimpleNamespace(secure=True, remote="forged",
        transport=SimpleNamespace(get_extra_info=lambda key: ("198.51.100.1", 1234)),
        headers=CIMultiDict({"X-Real-IP": "192.0.2.1", "X-Forwarded-Proto": "http"}))
    assert WebsitePublicPolicy.parse().client_ip(request) == "198.51.100.1"


@pytest.mark.parametrize("cidrs", ["0.0.0.0/0", "::/0", "127.0.0.1/8", "", "bad"])
def test_explicit_proxy_configuration(cidrs):
    if cidrs == "":
        assert WebsitePublicPolicy.parse().trusted_proxies == ()
    else:
        with pytest.raises(ValueError):
            WebsitePublicPolicy.parse(trusted_proxies=cidrs)
