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
async def test_fetch_integrations_error():
    client = ControlPlaneClient(base_url="http://control-plane", internal_token="token")
    client.client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await client.fetch_integrations()

    await client.close()
