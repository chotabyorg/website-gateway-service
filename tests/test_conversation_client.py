from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

from services.conversation_client import (
    ConversationClient,
    ConversationConflict,
    ConversationUnavailable,
    WebsiteAdmission,
    WebsiteEnvelope,
    WebsiteResponseIntent,
    response_attempt_for,
)

ENVELOPE = WebsiteEnvelope("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                           "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444",
                           "55555555-5555-4555-8555-555555555555", "  exact\ntext  ")
ADMISSION_ID = "66666666-6666-4666-8666-666666666666"
SOURCE_ATTEMPT_ID = "77777777-7777-4777-8777-777777777777"


def test_website_response_attempt_is_stable_and_scoped_to_admission():
    first = response_attempt_for(ADMISSION_ID)
    expected = str(uuid5(NAMESPACE_URL, f"main-chota.website-widget-response.v2:{ADMISSION_ID}"))
    assert first == expected == response_attempt_for(ADMISSION_ID)
    assert first != response_attempt_for("77777777-7777-4777-8777-777777777777")
    with pytest.raises(ValueError):
        response_attempt_for("not-a-canonical-uuid")


@pytest.mark.asyncio
async def test_client_sends_exact_trusted_admission_only():
    seen = []
    async def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"route": "MANAGED", "admission_id": ADMISSION_ID, "replayed": False})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://conversation") as http:
        client = ConversationClient("http://unused", "sentinel-dedicated-s2s")
        await client.client.aclose()
        client.client = http
        assert await client.admit(ENVELOPE, "MANAGED") == WebsiteAdmission(
            "MANAGED", ADMISSION_ID, False)
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
async def test_begin_response_preserves_and_validates_action_truth_table():
    client = ConversationClient("http://unused", "token")
    client._post = AsyncMock(return_value={
        "action": "REPLAY_COMPLETED", "response_attempt_id": "attempt",
        "replay_source_attempt_id": SOURCE_ATTEMPT_ID, "draft_text": "retained",
        "send": False, "replayed": True, "denial_reason": None,
    })
    result = await client.begin_response(ENVELOPE, "attempt")
    assert result == WebsiteResponseIntent(
        response_attempt_id="attempt", draft_text="retained", action="REPLAY_COMPLETED",
        send=False, replayed=True, replay_source_attempt_id=SOURCE_ATTEMPT_ID,
    )

    client._post = AsyncMock(return_value={
        "action": "SEND", "response_attempt_id": "attempt", "draft_text": "retained",
        "send": "true", "replayed": False, "denial_reason": None,
    })
    with pytest.raises(ConversationUnavailable):
        await client.begin_response(ENVELOPE, "attempt")
    await client.close()


@pytest.mark.parametrize("payload", [
    {"action": "SEND", "send": False, "replayed": False,
     "replay_source_attempt_id": None, "denial_reason": None},
    {"action": "REPLAY_COMPLETED", "send": False, "replayed": True,
     "replay_source_attempt_id": None, "denial_reason": None},
    {"action": "REPLAY_COMPLETED", "send": False, "replayed": True,
     "replay_source_attempt_id": "not-a-uuid", "denial_reason": None},
    {"action": "DENY", "send": True, "replayed": False,
     "replay_source_attempt_id": None, "denial_reason": "SUPERSEDED"},
    {"action": "DENY", "send": False, "replayed": False,
     "replay_source_attempt_id": SOURCE_ATTEMPT_ID, "denial_reason": "SUPERSEDED"},
    {"action": "DENY", "send": False, "replayed": False,
     "replay_source_attempt_id": None, "denial_reason": "unexpected"},
    {"action": "SEND", "send": True, "replayed": True,
     "replay_source_attempt_id": None, "denial_reason": None},
])
@pytest.mark.asyncio
async def test_begin_response_rejects_incoherent_action_fields(payload):
    client = ConversationClient("http://unused", "token")
    client._post = AsyncMock(return_value={
        "response_attempt_id": "attempt", "draft_text": "retained", **payload,
    })
    with pytest.raises(ConversationUnavailable):
        await client.begin_response(ENVELOPE, "attempt")
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


@pytest.mark.parametrize("body", [
    {"route": "MANAGED", "admission_id": "not-a-uuid", "replayed": False},
    {"route": "MANAGED", "admission_id": ADMISSION_ID, "replayed": "false"},
    {"route": "UNKNOWN", "admission_id": ADMISSION_ID, "replayed": False},
])
@pytest.mark.asyncio
async def test_admission_requires_typed_canonical_result(body):
    client = ConversationClient("http://unused", "token")
    client._post = AsyncMock(return_value=body)
    with pytest.raises(ConversationUnavailable):
        await client.admit(ENVELOPE, "MANAGED")
    await client.close()
