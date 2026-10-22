"""Same native-process evidence tests in both gateways; no Docker or provider I/O."""
import asyncio
import copy
import json
import os
import signal
import sys
from uuid import uuid4

import pytest

from services.writer_attestor import (
    Parent, WriterAttestor, AttestationUnavailable, registration_id, join_owned,
    validate_descriptor,
)
from services.legacy_drain import LegacyDrain

SCOPE = dict(owner_id="qa-owner", integration_id=str(uuid4()), agent_id=str(uuid4()), channel="TELEGRAM")


def descriptor(parent):
    return dict(contract_version=2, owner_id=SCOPE["owner_id"],
        integration_id=SCOPE["integration_id"], agent_id=SCOPE["agent_id"],
        conversation_id=str(uuid4()), provider_thread_id="123", bot_id=999,
        outbound_intent_id=str(uuid4()), outbound_ordinal=1, source_kind="OUTBOUND_MESSAGE",
        source_id=str(uuid4()), response_attempt_id=str(uuid4()), work_kind="SEND_CANDIDATE",
        supplier_instance_id=parent.supplier, process_incarnation_id=parent.process)


class DurablePeer:
    def __init__(self, lose_register=False, lose_witness=False):
        self.registrations, self.witnesses, self.calls = {}, {}, []
        self.registered, self.witnessed = asyncio.Event(), asyncio.Event()
        self.lose_register, self.lose_witness = lose_register, lose_witness
        self.witness_release = None

    async def post(self, route, payload):
        self.calls.append((route, copy.deepcopy(payload)))
        rid = payload["registration_id"]
        table = self.registrations if route == "register" else self.witnesses
        replayed = rid in table
        if replayed:
            assert table[rid] == payload
        table[rid] = copy.deepcopy(payload)
        (self.registered if route == "register" else self.witnessed).set()
        if route == "witness" and self.witness_release:
            await self.witness_release.wait()
        flag = "lose_" + route
        if getattr(self, flag):
            setattr(self, flag, False)
            raise TimeoutError("fake_lost_ack")
        return dict(contract_version=1, registration_id=rid,
                    state="CLOSED" if rid in self.witnesses else "OPEN", replayed=replayed)


CHILD = r"""
import asyncio, json, os, subprocess, sys
from services.writer_attestor import WriterAttestor, deny_sender_subprocesses
# Exercise actual protected process. There must never be a surviving provider sender.
deny_sender_subprocesses()
try:
    subprocess.Popen([sys.executable, '-c', 'raise SystemExit(9)'])
except RuntimeError:
    pass
else:
    raise AssertionError('sender subprocess was permitted')
try:
    os.fork()
except RuntimeError:
    pass
else:
    raise AssertionError('sender fork was permitted')
assert 'WRITER_PRODUCER_TOKEN' not in os.environ
assert 'WRITER_SUPERVISOR_TOKEN' not in os.environ
assert 'WRITER_PROOF_TOKEN' not in os.environ
async def run():
    client = WriterAttestor()
    desc = json.loads(os.environ['TEST_DESCRIPTOR'])
    original_secret = client.secret
    client.secret = 'wrong-local-capability'
    try:
        await client.register(desc)
    except RuntimeError:
        pass
    else:
        raise AssertionError('wrong IPC capability accepted')
    client.secret = original_secret
    await client.register(desc)
    os.write(int(os.environ['TEST_READY']), b'R')
    await asyncio.to_thread(os.read, int(os.environ['TEST_RELEASE']), 1)
    if os.environ['TEST_JOIN'] == '1':
        await client.close(desc)
asyncio.run(run())
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("joined,kill,lose_register,lose_witness", [
    (True, False, False, False), (False, False, False, False),
    (False, True, True, True), (True, False, True, True),
])
async def test_exact_owned_child_and_stable_lost_ack(joined, kill, lose_register, lose_witness):
    peer = DurablePeer(lose_register, lose_witness)
    parent = Parent(peer, [SCOPE])
    desc = descriptor(parent)
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    env = dict(os.environ, WRITER_ATTESTOR_SCOPES=json.dumps([SCOPE]),
        WRITER_PRODUCER_TOKEN="producer-secret-sentinel", WRITER_SUPERVISOR_TOKEN="supervisor-secret",
        WRITER_PROOF_TOKEN="proof-secret", TEST_DESCRIPTOR=json.dumps(desc),
        TEST_READY=str(ready_w), TEST_RELEASE=str(release_r), TEST_JOIN=str(int(joined)))
    run = asyncio.create_task(parent.run_child([sys.executable, "-c", CHILD], environment=env,
                                             pass_fds=(ready_w, release_r)))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(os.read, ready_r, 1), 5) == b"R"
        assert not parent.waited and not peer.witnesses
        if kill:
            parent.child.kill()
        else:
            os.write(release_w, b"G")
        await asyncio.wait_for(run, 5)
        assert parent.waited and parent.child.returncode is not None
        rid = registration_id(desc)
        witness = peer.witnesses[rid]
        assert witness["proof_kind"] == ("TASKS_FENCED_AND_JOINED" if joined else "PROCESS_TERMINATED")
        for route in ("register", "witness"):
            calls = [payload for name, payload in peer.calls if name == route]
            assert calls and all(payload == calls[0] for payload in calls)
        assert parent.slots[rid]["closed"] is True
        replacement = Parent(peer, [SCOPE])
        assert replacement.supplier != parent.supplier and replacement.process != parent.process
        assert not replacement.slots
    finally:
        if parent.child is not None and parent.child.returncode is None:
            parent.child.kill()
            await parent.child.wait()
        for fd in (ready_r, ready_w, release_r, release_w):
            os.close(fd)
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_observation_is_retained_before_ack_and_replacement_waits():
    peer = DurablePeer(lose_witness=True)
    parent = Parent(peer, [SCOPE])
    desc = descriptor(parent)
    rid = registration_id(desc)
    await parent.register(dict(registration_id=rid, descriptor=desc))
    peer.witness_release = asyncio.Event()
    task = asyncio.create_task(parent.run_child([sys.executable, "-c", "pass"]))
    await asyncio.wait_for(peer.witnessed.wait(), 5)
    assert parent.waited and not task.done()
    original = copy.deepcopy(parent.slots[rid]["witness"])
    assert original["proof_kind"] == "PROCESS_TERMINATED"
    peer.witness_release.set()
    await asyncio.wait_for(task, 5)
    assert parent.slots[rid]["witness"] == original


@pytest.mark.asyncio
async def test_parent_loss_before_join_creates_no_witness():
    peer = DurablePeer()
    parent = Parent(peer, [SCOPE])
    desc = descriptor(parent)
    rid = registration_id(desc)
    await parent.register(dict(registration_id=rid, descriptor=desc))
    # A new parent cannot attest the prior process from a retained registration.
    replacement = Parent(peer, [SCOPE])
    with pytest.raises(AttestationUnavailable):
        await replacement.close(rid, "PROCESS_TERMINATED")
    assert not peer.witnesses
    assert parent.slots[rid]["witness"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("supplier_instance_id", str(uuid4())),
    ("process_incarnation_id", str(uuid4())), ("agent_id", str(uuid4())),
    ("owner_id", "other-owner"), ("work_kind", "RECOVER_ONLY"), ("text", "secret")])
async def test_wrong_scope_identity_or_sensitive_fields_never_register(field, value):
    peer = DurablePeer()
    parent = Parent(peer, [SCOPE])
    desc = descriptor(parent)
    desc[field] = value
    with pytest.raises(AttestationUnavailable):
        await parent.register(dict(registration_id=registration_id(desc), descriptor=desc))
    assert not peer.calls and not parent.slots


@pytest.mark.asyncio
async def test_child_local_fence_cannot_reopen(monkeypatch):
    parent = Parent(DurablePeer(), [SCOPE])
    desc = descriptor(parent)
    for key, value in parent.child_environment("/unused", {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WRITER_ATTESTOR_SCOPES", json.dumps([SCOPE]))
    client = WriterAttestor()
    calls = []
    async def call(op, payload):
        calls.append(op)
        return dict(contract_version=1, registration_id=payload["registration_id"],
                    state="OPEN" if op == "register" else "CLOSED", replayed=False)
    client._call = call
    await client.register(desc)
    await client.close(desc)
    with pytest.raises(AttestationUnavailable):
        await client.register(desc)
    assert calls == ["register", "joined"]


@pytest.mark.asyncio
async def test_repeated_cancellation_retains_actual_ownership():
    entered, release = asyncio.Event(), asyncio.Event()
    async def original():
        entered.set()
        await release.wait()
        return 42
    outer = asyncio.create_task(join_owned(original()))
    await entered.wait()
    outer.cancel()
    await asyncio.sleep(0)
    outer.cancel()
    await asyncio.sleep(0)
    assert not outer.done()
    release.set()
    result = await outer
    assert result.value == 42 and result.cancelled


@pytest.mark.asyncio
async def test_drain_is_positive_join_not_timeout_and_preserves_fixed_generation():
    drain = LegacyDrain([])
    scope = dict(SCOPE, lifecycle_generation=7, drain_generation=3)
    entered, release = asyncio.Event(), asyncio.Event()
    async def original():
        entered.set()
        await release.wait()
    original_task = asyncio.create_task(drain.run(scope, original))
    await entered.wait()
    draining = asyncio.create_task(drain.activate(scope))
    await asyncio.sleep(0)
    assert drain.receipts()[0]["state"] == "DRAINING"
    with pytest.raises(AttestationUnavailable):
        await drain.run(scope, original)
    original_task.cancel()
    await asyncio.sleep(0)
    original_task.cancel()
    await asyncio.sleep(0)
    assert not draining.done()
    release.set()
    await asyncio.gather(original_task, return_exceptions=True)
    await draining
    receipt = drain.receipts()[0]
    assert receipt["state"] == "DRAINED"
    assert receipt["acquired_count"] == receipt["joined_count"] == 1
    assert receipt["active_count"] == 0 and receipt["current_process_only"] is True
    assert receipt["lifecycle_generation"] == 7 and receipt["drain_generation"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_first", [False, True])
async def test_actual_parent_crash_only_retains_already_durable_witness(durable_first):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    peer = DurablePeer()
    async def endpoint(request):
        assert request.headers["X-Writer-Producer-Token"] == "test-producer-token" * 2
        return web.json_response(await peer.post(request.match_info["route"], await request.json()))
    app = web.Application()
    app.router.add_post("/internal/v1/writer-attestations/{route}", endpoint)
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    parent_program = r'''
import asyncio, json, os, sys
from services.writer_attestor import Parent, Producer
async def run():
    p = Parent(Producer(os.environ['TEST_URL'], os.environ['WRITER_PRODUCER_TOKEN']),
               json.loads(os.environ['WRITER_ATTESTOR_SCOPES']))
    desc = json.loads(os.environ['TEST_DESCRIPTOR'])
    desc.update(supplier_instance_id=p.supplier, process_incarnation_id=p.process)
    env = dict(os.environ, TEST_DESCRIPTOR=json.dumps(desc))
    await p.run_child([sys.executable, '-c', os.environ['TEST_CHILD_CODE']], environment=env,
                     pass_fds=(int(os.environ['TEST_READY']), int(os.environ['TEST_RELEASE'])))
asyncio.run(run())
'''
    child_program = r'''
import asyncio, json, os
from services.writer_attestor import WriterAttestor, deny_sender_subprocesses
deny_sender_subprocesses()
async def run():
    c = WriterAttestor()
    d = json.loads(os.environ['TEST_DESCRIPTOR'])
    await c.register(d)
    if os.environ['TEST_JOIN'] == '1':
        await c.close(d)
    os.write(int(os.environ['TEST_READY']), b'R')
    await asyncio.to_thread(os.read, int(os.environ['TEST_RELEASE']), 1)
asyncio.run(run())
'''
    process = None
    async with TestServer(app) as server:
        env = dict(os.environ, WRITER_PRODUCER_TOKEN="test-producer-token" * 2,
            WRITER_ATTESTOR_SCOPES=json.dumps([SCOPE]),
            TEST_DESCRIPTOR=json.dumps(descriptor(Parent(peer, [SCOPE]))),
            TEST_READY=str(ready_w), TEST_RELEASE=str(release_r),
            TEST_URL=str(server.make_url("")), TEST_JOIN=str(int(durable_first)),
            TEST_CHILD_CODE=child_program)
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", parent_program,
                env=env, pass_fds=(ready_w, release_r))
            os.close(ready_w)
            ready_w = None
            assert await asyncio.wait_for(asyncio.to_thread(os.read, ready_r, 1), 5) == b"R"
            process.kill()
            await process.wait()
            os.write(release_w, b"G")
            # EOF proves the test's orphan helper exited and did not leave senders;
            # it is deliberately never uploaded as a production process witness.
            assert await asyncio.wait_for(asyncio.to_thread(os.read, ready_r, 1), 5) == b""
            assert len(peer.registrations) == 1
            assert len(peer.witnesses) == int(durable_first)
            if durable_first:
                assert next(iter(peer.witnesses.values()))["proof_kind"] == "TASKS_FENCED_AND_JOINED"
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            try:
                os.write(release_w, b"G")
            except OSError:
                pass
            for fd in (ready_r, ready_w, release_r, release_w):
                if fd is not None:
                    os.close(fd)
