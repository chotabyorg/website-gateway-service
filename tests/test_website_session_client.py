"""Bounded HTTPS private wire; uncertain acquisition is never retried by this client."""
import json
import ssl
from uuid import uuid4

import httpx
import pytest

from services.website_session_client import WebsiteSessionClient, WebsiteSessionError
from services.website_session_contracts import (
    WebsiteDeliveryBegin,
    WebsiteDeliveryComplete,
    WebsiteHistoryRequest,
    WebsiteSessionAccess,
    WebsiteSessionAdmission,
    WebsiteSessionCreate,
)

PEER = "conversation.internal.example"
URL = "https://" + PEER
SCOPE = dict(owner_id="owner", integration_id=str(uuid4()), agent_id=str(uuid4()), binding_generation=7)
TOKEN = "a" * 43
ACCESS = dict(SCOPE, session_id=str(uuid4()), session_token=TOKEN)


def descriptor():
    return dict(outbound_intent_id=str(uuid4()), outbound_ordinal=1,
        source_kind="OUTBOUND_MESSAGE", source_id=str(uuid4()), response_attempt_id=str(uuid4()),
        delivery_id=str(uuid4()))


def recovery(item, status="UNKNOWN", writer="LIVE"):
    return dict(delivery_id=item["delivery_id"], status=status, writer_state=writer)


def begin_request(item, recover=False):
    return WebsiteDeliveryBegin(**ACCESS, **item, supplier_instance_id=uuid4(),
        process_incarnation_id=uuid4(), recover_only=recover)


def client(respond):
    return WebsiteSessionClient(URL, "private-service-token", expected_peer=PEER,
                                transport=httpx.MockTransport(respond))


@pytest.mark.parametrize("url,peer", [
    ("http://" + PEER, PEER), (URL, "different.internal.example"),
    ("https://user:password@" + PEER, PEER), (URL + "/other", PEER),
    (URL + "?credential=value", PEER), (URL + "#fragment", PEER),
    (URL, ""), ("https://", PEER),
])
def test_only_exact_configured_https_origin_is_accepted(url, peer):
    with pytest.raises(ValueError, match="invalid_verified_conversation_peer"):
        WebsiteSessionClient(url, "token", expected_peer=peer)


@pytest.mark.asyncio
async def test_tls_verification_uses_hostname_and_trusted_ca(monkeypatch):
    seen = {}
    original = httpx.AsyncClient

    def capture(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", capture)
    value = WebsiteSessionClient(URL, "token", expected_peer=PEER)
    assert seen["verify"].check_hostname and seen["verify"].verify_mode == ssl.CERT_REQUIRED
    assert seen["follow_redirects"] is False and seen["trust_env"] is False
    await value.close()


@pytest.mark.asyncio
async def test_create_is_new_identity_without_visitor_input_and_hides_secret_repr():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(201, json=dict(session_id=str(uuid4()), session_token=TOKEN,
                                             expires_at="2026-10-23T00:00:00Z"))

    value = client(respond)
    created = await value.create(WebsiteSessionCreate(**SCOPE, client_ip="192.0.2.1"))
    await value.close()
    assert TOKEN not in repr(created)
    assert created.session_token.get_secret_value() == TOKEN
    assert json.loads(seen[0].content) == dict(SCOPE, client_ip="192.0.2.1")
    assert seen[0].url.path == "/internal/v1/website/sessions"


@pytest.mark.asyncio
async def test_credentials_only_in_private_json_exact_identity_and_no_legacy_route():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=dict(route="MANAGED", admission_id=str(uuid4()), replayed=False))

    value = client(respond)
    request = WebsiteSessionAdmission(**ACCESS, message_id=uuid4(), text=" exact\ninput ")
    assert TOKEN not in repr(request)
    await value.admit(request)
    await value.close()
    sent = seen[0]
    assert sent.url.path == "/internal/v1/website/sessions/admissions" and sent.url.query == b""
    assert TOKEN not in str(sent.url) and TOKEN not in str(sent.headers)
    body = json.loads(sent.content)
    assert body["session_token"] == TOKEN and body["text"] == request.text
    assert {key: body[key] for key in SCOPE} == SCOPE
    assert "desired_route" not in body


@pytest.mark.asyncio
async def test_oversized_escaped_request_has_zero_io():
    seen = []
    value = client(lambda request: seen.append(request))
    with pytest.raises(WebsiteSessionError) as caught:
        await value.admit(WebsiteSessionAdmission(**ACCESS, message_id=uuid4(), text="\x01" * 12000))
    await value.close()
    assert caught.value.status == 413 and seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(302, 503), (404, 404), (409, 409), (500, 503)])
async def test_error_statuses_are_bounded_and_redirects_never_forward_credentials(status, expected):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location": "https://different.example"},
                              text="private diagnostic " + TOKEN)

    value = client(respond)
    with pytest.raises(WebsiteSessionError) as caught:
        await value.work(WebsiteSessionAccess(**ACCESS))
    await value.close()
    assert caught.value.status == expected and len(seen) == 1
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)


@pytest.mark.asyncio
async def test_unknown_original_recovery_contains_no_frame_and_keeps_identity():
    item = descriptor()
    body = dict(state="RECOVER_ONLY", item=item, recovery=recovery(item), frame=None)
    value = client(lambda _: httpx.Response(200, json=body))
    result = await value.work(WebsiteSessionAccess(**ACCESS))
    await value.close()
    assert result.frame is None and result.recovery.status == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["frame-id", "actor", "send-on-recovery", "replayed-send"])
async def test_positive_begin_is_exact_original_frame_and_never_grants_recovery_send(invalid):
    item = descriptor()
    body = dict(state="ACQUIRED", item=item, send=True, replayed=invalid == "replayed-send",
        recovery=recovery(item, "WRITE_IN_PROGRESS"),
        frame=dict(delivery_id=str(uuid4()) if invalid == "frame-id" else item["delivery_id"],
                   actor="AGENT" if invalid == "actor" else "HUMAN", text="retained reply"))
    value = client(lambda _: httpx.Response(200, json=body))
    with pytest.raises(WebsiteSessionError):
        await value.begin(begin_request(item, recover=invalid == "send-on-recovery"))
    await value.close()


@pytest.mark.asyncio
async def test_positive_acquisition_and_original_completion_match_closed_wire():
    item = descriptor()
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        current = recovery(item, "WRITE_IN_PROGRESS" if len(sent) == 1 else "TRANSPORT_ACCEPTED",
                           "LIVE" if len(sent) == 1 else "ENDED")
        return httpx.Response(200, json=(dict(state="ACQUIRED", item=item, send=True, replayed=False,
            recovery=current, frame=dict(delivery_id=item["delivery_id"], actor="HUMAN", text="reply"))
            if len(sent) == 1 else dict(recovery=current, replayed=False)))

    value = client(respond)
    request = begin_request(item)
    assert (await value.begin(request)).send
    completed = WebsiteDeliveryComplete(**request.model_dump(), outcome="WRITE_COMPLETED",
        writer_end=dict(kind="TASK_JOINED", supplier_instance_id=request.supplier_instance_id,
                        process_incarnation_id=request.process_incarnation_id))
    result = await value.complete(completed)
    await value.close()
    assert result.recovery.writer_state == "ENDED"
    assert sent[0]["session_token"] == sent[1]["session_token"] == TOKEN


@pytest.mark.asyncio
async def test_uncertain_begin_transport_has_one_attempt_and_sanitized_exception():
    seen = []

    def respond(request):
        seen.append(request)
        raise httpx.ReadTimeout("private diagnostic " + TOKEN, request=request)

    value = client(respond)
    with pytest.raises(WebsiteSessionError) as caught:
        await value.begin(begin_request(descriptor()))
    await value.close()
    assert len(seen) == 1 and TOKEN not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b'{"state":"EMPTY","private":"secret"}',
    b'{"state":"EMPTY","state":"SEND_CANDIDATE"}',
    b'{"state":"EMPTY"}' + b' ' * 4096,
    b' ' * 65537,
], ids=["unknown-field", "duplicate-field", "oversized-metadata", "oversized-body"])
async def test_closed_json_and_full_raw_metadata_bound(body):
    value = client(lambda _: httpx.Response(200, content=body,
                                           headers={"Content-Type": "application/json"}))
    with pytest.raises(WebsiteSessionError):
        await value.work(WebsiteSessionAccess(**ACCESS))
    await value.close()


@pytest.mark.asyncio
async def test_history_page_preserves_cursor_and_public_ids_without_internal_fields():
    public_id = str(uuid4())
    body = dict(state="PAGE", items=[dict(id=public_id, actor="CUSTOMER", text="retained input",
                client_message_id=str(uuid4()))], next_cursor=public_id, oversized_message_id=None)
    value = client(lambda _: httpx.Response(200, json=body))
    result = await value.history(WebsiteHistoryRequest(**ACCESS, cursor=uuid4()))
    await value.close()
    assert str(result.items[0].id) == str(result.next_cursor) == public_id
