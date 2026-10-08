from unittest.mock import AsyncMock

import httpx
import pytest

from services.conversation_client import ConversationClient, ConversationConflict, ConversationUnavailable, WebsiteEnvelope

ENVELOPE = WebsiteEnvelope("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                           "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444",
                           "55555555-5555-4555-8555-555555555555", "  exact\ntext  ")


@pytest.mark.asyncio
async def test_client_sends_exact_trusted_admission_only():
    seen = []
    async def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"route": "MANAGED", "admission_id": "private-id", "replayed": False})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://conversation") as http:
        client = ConversationClient("http://unused", "sentinel-dedicated-s2s")
        await client.client.aclose()
        client.client = http
        assert await client.admit(ENVELOPE, "MANAGED") == "MANAGED"
    import json
    assert seen[0].url.path == "/internal/v1/website/admissions"
    assert json.loads(seen[0].content) == {**ENVELOPE.lookup(), "text": "  exact\ntext  ", "desired_route": "MANAGED"}


@pytest.mark.asyncio
async def test_poll_pending_then_retained_result():
    client = ConversationClient("http://unused", "token", result_wait=0.1, poll_interval=0.01)
    client._post = AsyncMock(side_effect=[{"state": "PENDING"}, {"state": "DRAFT_READY", "draft_text": "retained"}])
    assert await client.wait_result(ENVELOPE) == {"state": "DRAFT_READY", "draft_text": "retained"}
    assert client._post.await_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_poll_timeout_pending_does_not_dispatch():
    client = ConversationClient("http://unused", "token", result_wait=0)
    client._post = AsyncMock(return_value={"state": "PENDING"})
    assert await client.wait_result(ENVELOPE) == {"state": "PENDING"}
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,error", [(409, {"detail": "sentinel-response-echo"}, ConversationConflict),
                                               (503, {"detail": "sentinel-response-echo"}, ConversationUnavailable),
                                               (200, {"route": "unexpected"}, ConversationUnavailable),
                                               (200, [], ConversationUnavailable)])
async def test_no_implicit_disposition_on_error(status, body, error):
    client = ConversationClient("http://conversation", "token")
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)), base_url="http://conversation")
    with pytest.raises(error) as caught:
        await client.admit(ENVELOPE, "LEGACY")
    assert "sentinel-response-echo" not in str(caught.value)
    await client.close()
