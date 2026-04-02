import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

from services.core_api import CoreAPIClient


@pytest.mark.asyncio
async def test_send_message_success():
    client = CoreAPIClient(base_url="http://ai-agent-service")

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"answer": "Hello there!", "session_id": "sess-1"}
    client.client.post = AsyncMock(return_value=response)

    result = await client.send_message("agent-1", "sess-1", "Hi")

    assert result == "Hello there!"
    client.client.post.assert_awaited_once_with(
        "/v1/chat/completions",
        json={
            "agent_id": "agent-1",
            "external_client_id": "sess-1",
            "channel": "website_widget",
            "message": "Hi",
        },
    )

    await client.close()


@pytest.mark.asyncio
async def test_send_message_returns_fallback_on_error():
    client = CoreAPIClient(base_url="http://ai-agent-service")
    client.client.post = AsyncMock(side_effect=httpx.HTTPError("boom"))

    result = await client.send_message("agent-1", "sess-1", "Hi")

    assert "недоступен" in result

    await client.close()
