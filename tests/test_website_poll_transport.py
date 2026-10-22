"""Owned task semantics and real HTTP responses; the private authority is a fake."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio

from api import website_sessions
from api.handlers import handle_config
from api.middleware import cors_middleware, safe_error_middleware
from core.website_public_policy import WebsitePublicPolicy
from services.control_plane_client import WidgetValidation
from services.website_poll_transport import WebsitePollTransport
from services.website_session_client import WebsiteSessionError
from services.website_session_contracts import (
    WebsiteDeliveryBegun, WebsiteDeliveryCompleted, WebsiteDeliveryDescriptor,
    WebsiteDeliveryRecovery, WebsiteDeliveryWorkResult, WebsitePollEnded,
    WebsitePollStarted, WebsitePublicFrame, WebsiteSessionAccess,
)
from services.widget_key_manager import WidgetKeyManager

INTEGRATION, AGENT, SESSION = (str(uuid4()) for _ in range(3))
TOKEN = "a" * 43
ACCESS = dict(owner_id="trusted-owner", integration_id=INTEGRATION, agent_id=AGENT,
              binding_generation=7, session_id=SESSION, session_token=TOKEN)
HEADERS = {"X-Widget-Key": "wk_fixture", "X-Widget-Session-Token": TOKEN,
           "X-Forwarded-Proto": "https", "X-Real-IP": "192.0.2.1"}


class Peer:
    def __init__(self):
        self.calls, self.completed = [], []
        self.item = WebsiteDeliveryDescriptor(outbound_intent_id=uuid4(), outbound_ordinal=1,
            source_kind="OUTBOUND_MESSAGE", source_id=uuid4(), response_attempt_id=uuid4(),
            delivery_id=uuid4())
        self.frame = WebsitePublicFrame(delivery_id=self.item.delivery_id, actor="HUMAN",
                                       text="owner fixture")
        self.work_state, self.decision = "SEND_CANDIDATE", "ACQUIRED"
        self.begin_error, self.poll_error = None, None
        self.poll_state = "ACQUIRED"
        self.begin_entered = asyncio.Event()
        self.begin_release = None
        self.end_entered, self.end_done = asyncio.Event(), asyncio.Event()
        self.end_release = None

    def recovery(self, status="PENDING", writer="NONE"):
        return WebsiteDeliveryRecovery(delivery_id=self.item.delivery_id,
                                       status=status, writer_state=writer)

    async def begin_poll(self, command):
        self.calls.append(("begin_poll", command))
        if self.poll_error:
            raise self.poll_error
        if self.poll_state == "RECOVER_ONLY":
            return WebsitePollStarted(state="RECOVER_ONLY")
        return WebsitePollStarted(state="ACQUIRED", poll_id=uuid4(),
                                  deadline_at=datetime.now(timezone.utc) + timedelta(seconds=20))

    async def work(self, command):
        self.calls.append(("work", command))
        if self.work_state == "EMPTY":
            return WebsiteDeliveryWorkResult(state="EMPTY")
        if self.work_state == "SEND_CANDIDATE":
            return WebsiteDeliveryWorkResult(state=self.work_state, item=self.item, frame=self.frame)
        return WebsiteDeliveryWorkResult(state=self.work_state, item=self.item,
                                         recovery=self.recovery("UNKNOWN", "LIVE"))

    async def begin(self, command):
        self.calls.append(("begin", command))
        self.begin_entered.set()
        if self.begin_release:
            await self.begin_release.wait()
        if self.begin_error:
            raise self.begin_error
        acquired = self.decision == "ACQUIRED"
        return WebsiteDeliveryBegun(state=self.decision, item=self.item, send=acquired,
            replayed=self.decision == "RECOVER_ONLY",
            recovery=self.recovery("WRITE_IN_PROGRESS" if acquired else "UNKNOWN",
                                   "LIVE"), frame=self.frame if acquired else None)

    async def complete(self, command):
        self.calls.append(("complete", command))
        self.completed.append(command)
        return WebsiteDeliveryCompleted(recovery=self.recovery(
            "TRANSPORT_ACCEPTED" if command.outcome == "WRITE_COMPLETED" else "UNKNOWN", "ENDED"),
            replayed=False)

    async def end_poll(self, command):
        self.calls.append(("end_poll", command))
        self.end_entered.set()
        if self.end_release:
            await self.end_release.wait()
        self.end_done.set()
        return WebsitePollEnded(replayed=False)


@pytest_asyncio.fixture
async def public_poll():
    peer = Peer()
    transport = WebsitePollTransport(peer, wait_seconds=0.2, poll_interval=0.01,
                                    cleanup_timeout=0.3)
    cp = AsyncMock()
    cp.validate_widget_key.return_value = WidgetValidation(INTEGRATION, AGENT, "trusted-owner",
                                                          {}, True, 7)
    app = web.Application(middlewares=[safe_error_middleware, cors_middleware])
    app["public_session_policy"] = WebsitePublicPolicy.parse(
        json.dumps({INTEGRATION: AGENT}), "127.0.0.1/32")
    app["key_manager"] = WidgetKeyManager(cp)
    app["website_session_client"], app["website_poll_transport"] = peer, transport
    app.router.add_post("/v1/sessions/poll", website_sessions.poll)
    app.router.add_get("/v1/config", handle_config)
    async with TestClient(TestServer(app)) as client:
        yield SimpleNamespace(client=client, peer=peer, app=app, transport=transport, cp=cp)


async def poll(public):
    return await public.client.post("/v1/sessions/poll", json={"session_id": SESSION},
                                    headers=HEADERS)


@pytest.mark.asyncio
async def test_positive_begin_is_the_only_frame_and_original_write_is_joined(public_poll):
    public = public_poll
    result = await poll(public)
    assert result.status == 200
    assert await result.json() == {"state": "DELIVERY",
                                   "delivery": public.peer.frame.model_dump(mode="json")}
    await public.peer.end_done.wait()
    assert result.headers["Cache-Control"] == "no-store"
    assert result.headers["Access-Control-Allow-Origin"] == "*"
    assert [name for name, _ in public.peer.calls] == [
        "begin_poll", "work", "begin", "complete", "end_poll"]
    begin, done = public.peer.calls[2][1], public.peer.completed[0]
    assert done.outcome == "WRITE_COMPLETED" and done.error_code is None
    assert "poll_id" not in done.model_dump()
    assert done.writer_end.kind == "TASK_JOINED"
    assert done.supplier_instance_id == begin.supplier_instance_id
    assert done.process_incarnation_id == begin.process_incarnation_id
    assert done.session_token.get_secret_value() == TOKEN
    assert begin.owner_id == "trusted-owner" and begin.binding_generation == 7
    assert str(begin.outbound_intent_id) not in await result.text()
    assert TOKEN not in await result.text()


@pytest.mark.parametrize("state", ["RECOVER_ONLY", "NO_SEND", "FRAME_TOO_LARGE"])
@pytest.mark.asyncio
async def test_discovery_never_authorizes_text_when_begin_denies(public_poll, state):
    public_poll.peer.decision = state
    result = await poll(public_poll)
    body = await result.json()
    await public_poll.peer.end_done.wait()
    assert body["state"] == state and "delivery" not in body and "text" not in body
    assert public_poll.peer.frame.text not in await result.text()
    assert public_poll.peer.completed == []
    assert [name for name, _ in public_poll.peer.calls].count("begin") == 1


@pytest.mark.parametrize("state", ["RECOVER_ONLY", "FRAME_TOO_LARGE"])
@pytest.mark.asyncio
async def test_recovery_work_is_bounded_metadata_and_never_acquired(public_poll, state):
    public_poll.peer.work_state = state
    result = await poll(public_poll)
    raw = await result.read()
    await public_poll.peer.end_done.wait()
    assert len(raw) <= 4096 and b'"text"' not in raw
    assert public_poll.peer.frame.text.encode() not in raw
    assert json.loads(raw)["delivery_id"] == str(public_poll.peer.item.delivery_id)
    assert "begin" not in [name for name, _ in public_poll.peer.calls]
    assert not public_poll.peer.completed


@pytest.mark.asyncio
async def test_full_escaped_public_frame_is_sized_before_begin(public_poll):
    public_poll.peer.frame.text = "\x01" * 16000
    result = await poll(public_poll)
    assert (await result.json())["state"] == "FRAME_TOO_LARGE"
    await public_poll.peer.end_done.wait()
    assert "begin" not in [name for name, _ in public_poll.peer.calls]
    assert not public_poll.peer.completed


@pytest.mark.asyncio
async def test_lost_begin_ack_records_unknown_with_no_write_or_retry(public_poll):
    public_poll.peer.begin_error = WebsiteSessionError()
    result = await poll(public_poll)
    body = await result.json()
    await public_poll.peer.end_done.wait()
    assert body == {"state": "RECOVER_ONLY", "status": "UNKNOWN",
                    "delivery_id": str(public_poll.peer.item.delivery_id)}
    assert len(public_poll.peer.completed) == 1
    done = public_poll.peer.completed[0]
    assert done.outcome == "UNKNOWN" and done.writer_end.kind == "TASK_JOINED"
    assert [name for name, _ in public_poll.peer.calls].count("begin") == 1


@pytest.mark.asyncio
async def test_durable_poll_rate_denial_does_not_read_or_acquire_work(public_poll):
    public_poll.peer.poll_error = WebsiteSessionError(429, 17)
    result = await poll(public_poll)
    assert result.status == 429 and result.headers["Retry-After"] == "17"
    assert await result.json() == {"error": "Session unavailable"}
    assert [name for name, _ in public_poll.peer.calls] == ["begin_poll"]


@pytest.mark.asyncio
async def test_non_executable_poll_replay_never_reads_work_or_ends_someone_elses_poll(public_poll):
    public_poll.peer.poll_state = "RECOVER_ONLY"
    result = await poll(public_poll)
    assert await result.json() == {"state": "RECOVER_ONLY"}
    assert [name for name, _ in public_poll.peer.calls] == ["begin_poll"]


@pytest.mark.asyncio
async def test_completed_socket_is_not_resent_when_private_completion_reply_is_lost(public_poll):
    commands = []

    async def complete(command):
        commands.append(command)
        raise WebsiteSessionError()

    public_poll.peer.complete = complete
    result = await poll(public_poll)
    assert (await result.json())["state"] == "DELIVERY"
    await public_poll.peer.end_done.wait()
    assert len(commands) == 1 and commands[0].outcome == "WRITE_COMPLETED"
    assert [name for name, _ in public_poll.peer.calls].count("begin") == 1


@pytest.mark.asyncio
async def test_empty_poll_is_bounded_and_does_not_end_any_writer(public_poll):
    public_poll.peer.work_state = "EMPTY"
    started = asyncio.get_running_loop().time()
    result = await poll(public_poll)
    assert await result.json() == {"state": "EMPTY"}
    assert asyncio.get_running_loop().time() - started < 1
    await public_poll.peer.end_done.wait()
    assert not public_poll.peer.completed
    assert 1 <= [name for name, _ in public_poll.peer.calls].count("work") <= 25


@pytest.mark.asyncio
async def test_http_response_finishes_before_independent_private_cleanup(public_poll):
    public_poll.peer.end_release = asyncio.Event()
    try:
        result = await asyncio.wait_for(poll(public_poll), 0.25)
        assert (await result.json())["state"] == "DELIVERY"
        await public_poll.peer.end_entered.wait()
        assert not public_poll.peer.end_done.is_set()
    finally:
        public_poll.peer.end_release.set()
    await public_poll.peer.end_done.wait()


@pytest.mark.asyncio
async def test_poll_default_off_precedes_body_tls_and_credentials(public_poll):
    public_poll.app["website_poll_transport"] = None
    result = await public_poll.client.post("/v1/sessions/poll", data=b"bad-json")
    assert result.status == 404 and public_poll.peer.calls == []
    assert not public_poll.cp.mock_calls


@pytest.mark.asyncio
async def test_public_config_negotiates_only_enabled_current_common_protocol(public_poll):
    result = await public_poll.client.get("/v1/config?key=wk_fixture")
    body = await result.json()
    assert body["session_transport"] == "credential-v1"
    assert "owner_id" not in body and "binding_generation" not in body
    assert "session_token" not in body and "integration_id" not in body
    assert result.headers["Cache-Control"] == "no-store"
    assert public_poll.cp.validate_widget_key.await_count == 1
    public_poll.cp.validate_widget_key.return_value = WidgetValidation(INTEGRATION, AGENT,
        "trusted-owner", {}, False, 7)
    result = await public_poll.client.get("/v1/config?key=wk_fixture")
    assert "session_transport" not in await result.json()


@pytest.mark.parametrize("extra", [{"poll_id": str(uuid4())}, {"supplier_instance_id": str(uuid4())},
                                    {"message_id": str(uuid4())}, {"cursor": str(uuid4())}])
@pytest.mark.asyncio
async def test_browser_cannot_select_private_poll_or_delivery_identity(public_poll, extra):
    result = await public_poll.client.post("/v1/sessions/poll", headers=HEADERS,
                                           json={"session_id": SESSION, **extra})
    assert result.status == 400 and not public_poll.peer.calls and not public_poll.cp.mock_calls


def request():
    return SimpleNamespace(headers={}, transport=SimpleNamespace(close=lambda: None))


@pytest.mark.asyncio
async def test_cancel_during_original_begin_joins_reply_before_unknown_completion():
    peer = Peer()
    peer.begin_release = asyncio.Event()
    transport = WebsitePollTransport(peer, wait_seconds=1)
    writes = []

    async def write(*_):
        writes.append(True)

    transport._write = write
    task = asyncio.create_task(transport.run(request(), WebsiteSessionAccess(**ACCESS)))
    await peer.begin_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not peer.completed
    peer.begin_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writes == [] and len(peer.completed) == 1
    assert peer.completed[0].outcome == "UNKNOWN"
    assert peer.completed[0].writer_end.kind == "TASK_JOINED"
    assert peer.end_done.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_write_waits_for_original_positive_result():
    peer = Peer()
    transport = WebsitePollTransport(peer, wait_seconds=1)
    entered, release, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def write(*_):
        entered.set()
        await release.wait()
        ended.set()

    transport._write = write
    task = asyncio.create_task(transport.run(request(), WebsiteSessionAccess(**ACCESS)))
    await entered.wait()
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not peer.completed and not ended.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set() and len(peer.completed) == 1
    assert peer.completed[0].outcome == "WRITE_COMPLETED"
    assert peer.completed[0].writer_end.kind == "TASK_JOINED"


@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
@pytest.mark.asyncio
async def test_uncertain_original_socket_is_joined_and_never_blindly_retried(failure):
    peer = Peer()
    transport = WebsitePollTransport(peer, wait_seconds=0.15)
    joined, calls = asyncio.Event(), []

    async def write(*_):
        calls.append(True)
        try:
            if failure == "disconnect":
                raise ConnectionResetError()
            await asyncio.sleep(10)
        finally:
            joined.set()

    async def complete(command):
        assert joined.is_set()
        peer.completed.append(command)

    peer.complete, transport._write = complete, write
    await transport.run(request(), WebsiteSessionAccess(**ACCESS))
    assert calls == [True] and len(peer.completed) == 1
    done = peer.completed[0]
    assert done.outcome == "UNKNOWN" and done.writer_end.kind == "TASK_JOINED"
    assert done.error_code == ("TIMEOUT" if failure == "timeout" else "TRANSPORT_ERROR")
    assert [name for name, _ in peer.calls].count("begin") == 1


def test_restart_uses_new_identity_and_never_synthesizes_predecessor_join():
    one, two = WebsitePollTransport(Peer()), WebsitePollTransport(Peer())
    assert one.process_incarnation_id != two.process_incarnation_id
    assert one.supplier_instance_id != two.supplier_instance_id


@pytest.mark.parametrize("seconds", [0, 20.1, float("inf"), float("nan")])
def test_public_poll_cannot_extend_the_twenty_second_bound(seconds):
    with pytest.raises(ValueError):
        WebsitePollTransport(Peer(), wait_seconds=seconds)


def test_poll_configuration_defaults_off_and_requires_valid_common_cohort(load_module):
    from pydantic import ValidationError
    config = load_module("core.config")
    assert config.settings.COMMON_WEBSITE_POLL_ENABLED is False
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None, COMMON_WEBSITE_POLL_ENABLED=True)
    for seconds in (0, 20.01, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            config.Settings(_env_file=None, COMMON_WEBSITE_POLL_WAIT_SECONDS=seconds)
    common = json.dumps({INTEGRATION: AGENT})
    selected = config.Settings(_env_file=None, COMMON_WEBSITE_INTEGRATIONS=common,
        MANAGED_WEBSITE_GUARDED_INTEGRATIONS=common,
        CONVERSATION_SERVICE_URL="http://old-conversation", CONVERSATION_S2S_TOKEN="fake-v2",
        COMMON_WEBSITE_CONVERSATION_URL="https://conversation.internal.example",
        COMMON_WEBSITE_CONVERSATION_PEER="conversation.internal.example",
        COMMON_WEBSITE_S2S_TOKEN="fake-private", COMMON_WEBSITE_POLL_ENABLED=True,
        COMMON_WEBSITE_POLL_WAIT_SECONDS=0.1)
    assert selected.COMMON_WEBSITE_POLL_WAIT_SECONDS == 0.1
