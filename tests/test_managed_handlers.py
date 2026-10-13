import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from api.handlers import handle_chat
from api.middleware import cors_middleware, safe_error_middleware
from core.routing import RoutingPolicy
from services.conversation_client import (
    ConversationConflict,
    ConversationUnavailable,
    WebsiteResponseIntent,
    response_attempt_for,
)
from services.widget_key_manager import WidgetInfo

INTEGRATION = "8d4a1b73-7973-4dcc-a3d4-1312d871c1d4"
AGENT = "0b750714-af8d-4d8c-8fdc-4ddfeda98af9"
OWNER = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
MESSAGE = "33333333-3333-4333-8333-333333333333"


def make_app(*, selected=True, mode="SESSION_ALLOWLIST", agent=AGENT, integration=INTEGRATION):
    policy = RoutingPolicy.parse(json.dumps({INTEGRATION: AGENT}),
          json.dumps({INTEGRATION: {"expected_agent_id": AGENT, "mode": mode,
                                    "session_ids": [] if mode == "INTEGRATION_ALL" else [SESSION]}}) if selected else '{}')
    key = MagicMock()
    key.validate_key_for_chat = AsyncMock(return_value=WidgetInfo(integration, agent, OWNER, "fake-key", {}))
    legacy = AsyncMock()
    legacy.send_message.return_value = "legacy"
    conversation = AsyncMock()
    conversation.admit.return_value = "MANAGED"
    conversation.wait_result.return_value = {"state": "DRAFT_READY", "draft_text": "retained"}
    conversation.begin_response.return_value = WebsiteResponseIntent(
        response_attempt_id="attempt", draft_text="retained", send=True, replayed=False,
    )
    app = web.Application(middlewares=[safe_error_middleware, cors_middleware])
    app.update(key_manager=key, core_api=legacy, conversation_client=conversation, routing_policy=policy)
    app.router.add_post('/v1/chat', handle_chat)
    return app, legacy, conversation


async def post(client, **overrides):
    payload = {"session_id": SESSION, "message_id": MESSAGE, "message": "  identical\ntext  "}
    payload.update(overrides)
    return await client.post('/v1/chat', json=payload, headers={"X-Widget-Key": "fake-key", "Origin": "https://client.example"})


@pytest.mark.asyncio
async def test_managed_success_intent_before_write_cors_and_evidence():
    app, legacy, cs = make_app()
    async with TestClient(TestServer(app)) as client:
        response = await post(client, owner_id="public-forgery", integration_id="public-forgery", agent_id="public-forgery")
        assert response.status == 200
        assert await response.json() == {"reply": "retained", "session_id": SESSION}
        assert response.headers['Access-Control-Allow-Origin'] == 'https://client.example'
        assert response.headers['Cache-Control'] == 'no-store'
    legacy.send_message.assert_not_awaited()
    envelope, desired = cs.admit.call_args.args
    assert desired == 'MANAGED'
    assert (envelope.owner_id, envelope.integration_id, envelope.agent_id) == (OWNER, INTEGRATION, AGENT)
    assert envelope.text == '  identical\ntext  '
    attempt = cs.begin_response.call_args.args[1]
    assert attempt == response_attempt_for(MESSAGE)
    cs.complete_response.assert_awaited_once_with(envelope, attempt, 'WRITE_COMPLETED')


@pytest.mark.asyncio
async def test_send_false_never_exposes_stale_draft_or_records_completion():
    app, legacy, cs = make_app()
    cs.begin_response.return_value = WebsiteResponseIntent(
        response_attempt_id="attempt", draft_text="stale secret reply",
        send=False, replayed=False, denial_reason="SUPERSEDED",
    )
    async with TestClient(TestServer(app)) as client:
        response = await post(client)
        assert response.status == 409
        payload = await response.json()
        assert payload == {"error": "response_superseded", "session_id": SESSION}
        assert "stale secret reply" not in await response.text()
        assert "reply" not in payload
        assert response.headers["Cache-Control"] == "no-store"
    cs.complete_response.assert_not_awaited()
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_transport_replay_returns_retryable_error_without_draft():
    app, legacy, cs = make_app()
    cs.begin_response.return_value = WebsiteResponseIntent(
        response_attempt_id="attempt", draft_text="uncertain secret reply",
        send=False, replayed=True, denial_reason="DELIVERY_NOT_RETRYABLE",
    )
    async with TestClient(TestServer(app)) as client:
        response = await post(client)
        assert response.status == 503
        payload = await response.json()
        assert payload == {"error": "response_unavailable", "session_id": SESSION}
        assert "uncertain secret reply" not in await response.text()
    cs.complete_response.assert_not_awaited()
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['SESSION_ALLOWLIST', 'INTEGRATION_ALL'])
@pytest.mark.parametrize('overrides', [{"message_id": None}, {"message_id": "bad"}, {"session_id": "opaque"}, {"message": ["bad"]}])
async def test_managed_requires_stable_uuid_identity(mode, overrides):
    app, legacy, cs = make_app(mode=mode)
    async with TestClient(TestServer(app)) as client:
        response = await post(client, **overrides)
        # Invalid opaque allowlist session is unselected, but identified guarded
        # traffic still requires a valid full envelope to arbitrate rollback.
        assert response.status == 400
    legacy.send_message.assert_not_awaited()
    cs.admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_session_missing_message_id_denied():
    app, legacy, cs = make_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post('/v1/chat', json={"session_id": SESSION, "message": "hi"}, headers={"X-Widget-Key": "fake-key"})
        assert response.status == 400
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_session_without_message_id_stays_legacy():
    app, legacy, cs = make_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post('/v1/chat', json={"session_id": "opaque-legacy", "message": "hi"}, headers={"X-Widget-Key": "fake-key"})
        assert response.status == 200 and (await response.json())["reply"] == "legacy"
    cs.admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_integration_stays_legacy_even_with_ids():
    app, legacy, cs = make_app(mode='INTEGRATION_ALL', integration=str(uuid4()))
    async with TestClient(TestServer(app)) as client:
        response = await post(client)
        assert response.status == 200 and (await response.json())["reply"] == "legacy"
    cs.admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_guarded_agent_reassignment_fails_closed():
    app, legacy, cs = make_app(agent=str(uuid4()))
    async with TestClient(TestServer(app)) as client:
        assert (await post(client)).status == 503
    cs.admit.assert_not_awaited()
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_retained_managed_disposition_dominates_new_legacy_policy():
    app, legacy, cs = make_app(selected=False)
    async with TestClient(TestServer(app)) as client:
        response = await post(client)
        assert response.status == 200 and (await response.json())["reply"] == 'retained'
    assert cs.admit.call_args.args[1] == 'LEGACY'
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_legacy_disposition_wins_policy_race():
    app, legacy, cs = make_app()
    cs.admit.return_value = 'LEGACY'
    async with TestClient(TestServer(app)) as client:
        response = await post(client)
        assert response.status == 200 and (await response.json())["reply"] == 'legacy'
    cs.wait_result.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['admit', 'wait_result', 'begin_response'])
async def test_conversation_outage_never_legacy_fallback(phase):
    app, legacy, cs = make_app()
    getattr(cs, phase).side_effect = ConversationUnavailable('safe-code')
    async with TestClient(TestServer(app)) as client:
        assert (await post(client)).status == 503
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_content_conflict_returns409():
    app, legacy, cs = make_app()
    cs.admit.side_effect = ConversationConflict('safe')
    async with TestClient(TestServer(app)) as client:
        assert (await post(client)).status == 409
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['PENDING', 'UNKNOWN', 'NO_RESPONSE', 'FAILED'])
async def test_nonready_result_never_execution_fallback(state):
    app, legacy, cs = make_app()
    cs.wait_result.return_value = {"state": state, "draft_text": None}
    async with TestClient(TestServer(app)) as client:
        assert (await post(client)).status == 503
    legacy.send_message.assert_not_awaited()
    cs.begin_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_retries_reuse_stable_transport_identity():
    app, legacy, cs = make_app()
    async with TestClient(TestServer(app)) as client:
        first = await post(client)
        second = await post(client)
        assert await first.json() == await second.json()
    assert cs.admit.call_args_list[0].args[0] == cs.admit.call_args_list[1].args[0]
    assert cs.complete_response.call_args_list[0].args[1] == cs.complete_response.call_args_list[1].args[1]
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_evidence_write_keeps_physical_reply_and_no_legacy():
    app, legacy, cs = make_app()
    cs.complete_response.side_effect = ConversationUnavailable('safe')
    async with TestClient(TestServer(app)) as client:
        response = await post(client)
        assert response.status == 200 and (await response.json())['reply'] == 'retained'
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_identical_text_distinct_message_ids_are_distinct_envelopes():
    app, legacy, cs = make_app()
    async with TestClient(TestServer(app)) as client:
        first = await post(client)
        second = await post(client, message_id=str(uuid4()))
        assert first.status == second.status == 200
    first_envelope = cs.admit.call_args_list[0].args[0]
    second_envelope = cs.admit.call_args_list[1].args[0]
    assert first_envelope.text == second_envelope.text
    assert first_envelope.message_id != second_envelope.message_id


@pytest.mark.asyncio
async def test_socket_write_failure_records_failed_fact(monkeypatch):
    import api.handlers as handlers
    from aiohttp.test_utils import make_mocked_request
    app, legacy, cs = make_app()
    request = make_mocked_request('POST', '/v1/chat', app=app)
    from services.conversation_client import WebsiteEnvelope
    envelope = WebsiteEnvelope(OWNER, INTEGRATION, AGENT, SESSION, MESSAGE, 'exact')
    monkeypatch.setattr(web.Response, 'prepare', AsyncMock(side_effect=ConnectionResetError('secret-response-data')))
    response = await handlers._managed_reply(request, cs, envelope)
    assert response.status == 200
    assert cs.complete_response.call_args.args[2] == 'WRITE_FAILED'
    legacy.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_socket_write_joins_unknown_reconciliation(monkeypatch):
    import asyncio
    import api.handlers as handlers
    from aiohttp.test_utils import make_mocked_request
    from services.conversation_client import WebsiteEnvelope
    app, legacy, cs = make_app()
    request = make_mocked_request('POST', '/v1/chat', app=app)
    envelope = WebsiteEnvelope(OWNER, INTEGRATION, AGENT, SESSION, MESSAGE, 'exact')
    monkeypatch.setattr(web.Response, 'prepare', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await handlers._managed_reply(request, cs, envelope)
    cs.complete_response.assert_awaited_once()
    assert cs.complete_response.call_args.args[2] == 'UNKNOWN'


@pytest.mark.asyncio
async def test_repeated_request_cancellation_joins_bounded_evidence_task():
    import asyncio
    import api.handlers as handlers
    cs = AsyncMock()
    entered, finish = asyncio.Event(), asyncio.Event()
    async def complete(*args):
        entered.set()
        await finish.wait()
    cs.complete_response.side_effect = complete
    task = asyncio.create_task(handlers._complete_transport(cs, None, MESSAGE, 'UNKNOWN', timeout=0.2))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cs.complete_response.await_count == 1
