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
async def test_send_message_preserves_safe_markdown_answer():
    client = CoreAPIClient(base_url="http://ai-agent-service")

    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "answer": "**Hello**\n\n[Docs](https://example.com)\nUse <name>",
        "session_id": "sess-1",
    }
    client.client.post = AsyncMock(return_value=response)

    result = await client.send_message("agent-1", "sess-1", "Hi")

    assert result == "**Hello**\n\n[Docs](https://example.com)\nUse <name>"

    await client.close()


@pytest.mark.asyncio
async def test_send_message_returns_fallback_on_error():
    client = CoreAPIClient(base_url="http://ai-agent-service")
    client.client.post = AsyncMock(side_effect=httpx.HTTPError("boom"))

    result = await client.send_message("agent-1", "sess-1", "Hi")

    assert "недоступен" in result

    await client.close()


def test_client_uses_configured_read_timeout(load_module, monkeypatch):
    monkeypatch.setenv("AI_AGENT_REQUEST_TIMEOUT_SECONDS", "75")
    core_api = load_module("services.core_api")

    client = core_api.CoreAPIClient(base_url="http://ai-agent-service")

    assert client.client.timeout.connect == 5.0
    assert client.client.timeout.read == 75.0
    assert client.client.timeout.write == 30.0
    assert client.client.timeout.pool == 10.0


@pytest.mark.asyncio
async def test_send_message_returns_fallback_on_timeout():
    client = CoreAPIClient(base_url="http://ai-agent-service")
    client.client.post = AsyncMock(side_effect=httpx.ReadTimeout("too slow"))

    result = await client.send_message("agent-1", "sess-1", "Hi")

    assert "слишком много времени" in result

    await client.close()
