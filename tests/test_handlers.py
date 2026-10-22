import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.handlers import handle_chat, handle_config, handle_health, handle_root
from api.middleware import cors_middleware
from services.widget_key_manager import WidgetInfo


def _make_app(key_manager=None, core_api=None) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["key_manager"] = key_manager or MagicMock()
    app["core_api"] = core_api or AsyncMock()
    app.router.add_post("/v1/chat", handle_chat)
    app.router.add_get("/v1/config", handle_config)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_root)
    return app


def _make_widget_info() -> WidgetInfo:
    return WidgetInfo(
        integration_id="int-1",
        agent_id="agent-1",
        owner_id="owner-1",
        widget_api_key="wk_abc",
        config={},
    )


def _make_key_manager(value=None):
    key_manager = MagicMock()
    key_manager.validate_key.return_value = value
    key_manager.validate_key_for_chat = AsyncMock(return_value=value)
    return key_manager


@pytest.mark.asyncio
async def test_chat_success():
    key_manager = _make_key_manager(_make_widget_info())
    key_manager.active_count.return_value = 1

    core_api = AsyncMock()
    core_api.send_message.return_value = "Hello!"

    app = _make_app(key_manager=key_manager, core_api=core_api)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat",
            json={"session_id": "sess-1", "message": "Hi"},
            headers={"X-Widget-Key": "wk_abc"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["reply"] == "Hello!"
        assert data["session_id"] == "sess-1"

    key_manager.validate_key_for_chat.assert_awaited_once_with("wk_abc")
    core_api.send_message.assert_awaited_once_with(
        "agent-1", "sess-1", "Hi", owner_id="owner-1"
    )


@pytest.mark.asyncio
async def test_chat_response_shape_is_unchanged_for_formatted_reply():
    key_manager = _make_key_manager(_make_widget_info())

    core_api = AsyncMock()
    core_api.send_message.return_value = "Hello\n- item"

    app = _make_app(key_manager=key_manager, core_api=core_api)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat",
            json={"session_id": "sess-1", "message": "Hi"},
            headers={"X-Widget-Key": "wk_abc"},
        )

        assert resp.status == 200
        assert await resp.json() == {"reply": "Hello\n- item", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_chat_missing_key():
    app = _make_app()

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat",
            json={"session_id": "sess-1", "message": "Hi"},
        )
        assert resp.status == 401


@pytest.mark.asyncio
async def test_chat_invalid_key():
    key_manager = _make_key_manager(None)

    app = _make_app(key_manager=key_manager)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat",
            json={"session_id": "sess-1", "message": "Hi"},
            headers={"X-Widget-Key": "wk_invalid"},
        )
        assert resp.status == 401


@pytest.mark.asyncio
async def test_chat_missing_message():
    key_manager = _make_key_manager(_make_widget_info())

    app = _make_app(key_manager=key_manager)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat",
            json={"session_id": "sess-1"},
            headers={"X-Widget-Key": "wk_abc"},
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_chat_missing_session_id():
    key_manager = _make_key_manager(_make_widget_info())

    app = _make_app(key_manager=key_manager)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat",
            json={"message": "Hi"},
            headers={"X-Widget-Key": "wk_abc"},
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_config_success():
    key_manager = MagicMock()
    key_manager.validate_key.return_value = WidgetInfo(
        integration_id="int-1",
        agent_id="agent-1",
        owner_id="owner-1",
        widget_api_key="wk_abc",
        config={
            "primary_color": "#FF0000",
            "position": "bottom-left",
            "welcome_message": "Hello!",
            "window_title": "Support",
        },
    )
    app = _make_app(key_manager=key_manager)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/config?key=wk_abc")
        assert resp.status == 200
        data = await resp.json()
        assert data["primary_color"] == "#FF0000"
        assert data["position"] == "bottom-left"
        assert data["welcome_message"] == "Hello!"
        assert data["window_title"] == "Support"


@pytest.mark.asyncio
async def test_config_missing_key():
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/config")
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Missing key parameter"


@pytest.mark.asyncio
async def test_config_invalid_key():
    key_manager = MagicMock()
    key_manager.validate_key.return_value = None
    app = _make_app(key_manager=key_manager)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/config?key=wk_invalid")
        assert resp.status == 404
        data = await resp.json()
        assert data["error"] == "Invalid widget key"


@pytest.mark.asyncio
async def test_health():
    key_manager = MagicMock()
    key_manager.active_count.return_value = 3

    app = _make_app(key_manager=key_manager)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["active_widgets"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("enrolled", [False, True])
async def test_exact_drain_blocks_raw_and_managed_v1_before_parsing_or_admission(monkeypatch, enrolled):
    from api import handlers
    info = _make_widget_info()
    info.conversation_required = enrolled
    monkeypatch.setattr(handlers.legacy_drain, "configured", [dict(integration_id=info.integration_id,
                                                                  channel="WEBSITE_WIDGET")])
    core = AsyncMock()
    app = _make_app(_make_key_manager(info), core)
    app["conversation_client"] = AsyncMock()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat", data="invalid-json-no-admission",
                                     headers={"X-Widget-Key": "wk_abc"})
        assert response.status == 503
        assert response.headers["Cache-Control"] == "no-store"
    core.send_message.assert_not_awaited()
    app["conversation_client"].admit.assert_not_awaited()
