import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

from services.control_plane_client import ControlPlaneClient


@pytest.mark.asyncio
async def test_fetch_integrations_success():
    client = ControlPlaneClient(base_url="http://control-plane", internal_token="token")

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = [
        {
            "integration_id": "int-1",
            "agent_id": "agent-1",
            "owner_id": "owner-1",
            "credentials": {"widget_api_key": "wk_abc123", "widget_config": {"theme": "dark"}},
        }
    ]
    client.client.get = AsyncMock(return_value=response)

    integrations = await client.fetch_integrations()

    assert len(integrations) == 1
    assert integrations[0].integration_id == "int-1"
    assert integrations[0].agent_id == "agent-1"
    assert integrations[0].owner_id == "owner-1"
    assert integrations[0].credentials["widget_api_key"] == "wk_abc123"
    client.client.get.assert_awaited_once_with(
        "/v1/internal/integrations",
        params={"channel_type": "WEBSITE_WIDGET"},
    )

    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("routing", [
    {},
    {"conversation_required": "false", "binding_generation": 1},
    {"conversation_required": 0, "binding_generation": 1},
    {"conversation_required": None, "binding_generation": 1},
    {"conversation_required": False, "binding_generation": True},
    {"conversation_required": False, "binding_generation": 0},
    {"conversation_required": False, "binding_generation": "1"},
])
async def test_widget_validation_requires_strict_durable_routing(routing):
    client = ControlPlaneClient(base_url="http://control-plane", internal_token="token")
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "integration_id": "int-1", "agent_id": "agent-1", "owner_id": "owner-1",
        "config": {}, **routing,
    }
    client.client.get = AsyncMock(return_value=response)
    try:
        with pytest.raises(httpx.HTTPError, match="^Widget validation unavailable$"):
            await client.validate_widget_key("wk_fixture")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fetch_integrations_error():
    client = ControlPlaneClient(base_url="http://control-plane", internal_token="token")
    client.client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await client.fetch_integrations()

    await client.close()


@pytest.mark.asyncio
async def test_widget_key_validation_returns_authenticated_owner():
    client = ControlPlaneClient(base_url="http://control-plane", internal_token="token")
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "integration_id": "int-1",
        "agent_id": "agent-1",
        "owner_id": "owner-1",
        "config": {"primary_color": "#123456"},
        "conversation_required": True,
        "binding_generation": 3,
    }
    client.client.get = AsyncMock(return_value=response)

    validated = await client.validate_widget_key("wk_abc")

    assert validated is not None
    assert validated.owner_id == "owner-1"
    assert validated.conversation_required is True and validated.binding_generation == 3
    client.client.get.assert_awaited_once_with(
        "/v1/widget/internal/validate",
        params={"api_key": "wk_abc"},
    )
    await client.close()
