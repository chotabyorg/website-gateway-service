"""Fresh private CP routing survives removed gateway cohorts and public forgery."""
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.handlers import handle_chat
from core.routing import RoutingPolicy
from services.control_plane_client import ControlPlaneClient
from services.widget_key_manager import WidgetKeyManager

INTEGRATION = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True])
async def test_enrolled_widget_never_reaches_legacy_or_v2(guarded):
    import json
    cp = ControlPlaneClient("http://control-plane", "fixture-only")
    await cp.client.aclose()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"integration_id": INTEGRATION,
            "agent_id": AGENT, "owner_id": "owner", "config": {},
            "conversation_required": True, "binding_generation": 5})

    cp.client = httpx.AsyncClient(base_url="http://control-plane",
                                transport=httpx.MockTransport(respond))
    core, conversation = AsyncMock(), AsyncMock()
    app = web.Application()
    app["key_manager"] = WidgetKeyManager(cp)
    app["core_api"], app["conversation_client"] = core, conversation
    app["routing_policy"] = RoutingPolicy.parse(json.dumps({INTEGRATION: AGENT})
                                                if guarded else "{}")
    app.router.add_post("/v1/chat", handle_chat)
    try:
        async with TestClient(TestServer(app)) as client:
            for _ in range(2):
                response = await client.post("/v1/chat", headers={"X-Widget-Key": "wk_fixture"},
                    json={"session_id": "old-opaque", "message": "fixture",
                          "conversation_required": False, "binding_generation": 0})
                assert response.status == 409
                assert await response.json() == {
                    "error": "Conversation session required", "code": "session_required"}
                assert response.headers["Cache-Control"] == "no-store"
        assert calls == ["/v1/widget/internal/validate"] * 2
        assert not core.mock_calls and not conversation.mock_calls
    finally:
        await cp.close()
