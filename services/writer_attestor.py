"""Paired gateway lifecycle protocol. Producer credentials exist only in PID1.

This is process/task evidence, never delivery authority. Keep this file identical
in Telegram and Website. A parent loss before a durable witness remains UNKNOWN.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

import aiohttp
from aiohttp import web


class AttestationUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("writer_attestation_unavailable")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def scopes(name="WRITER_ATTESTOR_SCOPES"):
    try:
        result = json.loads(os.environ.get(name, "[]"))
        if not isinstance(result, list) or len(result) > 64:
            raise ValueError()
        for row in result:
            fields = {"owner_id", "integration_id", "agent_id", "channel"}
            if name == "WRITER_LEGACY_DRAIN_SCOPES":
                fields |= {"lifecycle_generation", "drain_generation"}
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError()
            if (not isinstance(row["owner_id"], str) or not row["owner_id"].strip()
                    or row["owner_id"] == "*" or len(row["owner_id"]) > 255
                    or row["channel"] not in {"TELEGRAM", "WEBSITE_WIDGET"}):
                raise ValueError()
            for key in ("integration_id", "agent_id"):
                if str(UUID(row[key])) != row[key]:
                    raise ValueError()
            for key in ("lifecycle_generation", "drain_generation"):
                if key in row and (type(row[key]) is not int or row[key] < 1):
                    raise ValueError()
        if len({(r["channel"], r["integration_id"]) for r in result}) != len(result):
            raise ValueError()
        return result
    except Exception:
        raise AttestationUnavailable() from None


def matches(row, descriptor):
    channel = descriptor.get("channel", "TELEGRAM")
    return (row["channel"] == channel and all(
        row[key] == str(descriptor.get(key, ""))
        for key in ("owner_id", "integration_id", "agent_id")))


def registration_id(descriptor):
    return str(uuid5(NAMESPACE_URL, "chota.writer-registration.v1:" + canonical(descriptor)))


def validate_descriptor(value, supplier, process, allowed):
    common = {"contract_version", "owner_id", "integration_id", "agent_id",
              "conversation_id", "supplier_instance_id", "process_incarnation_id",
              "outbound_intent_id", "outbound_ordinal", "source_kind", "source_id",
              "response_attempt_id"}
    website = value.get("channel") == "WEBSITE_WIDGET"
    if website:
        common.remove("conversation_id")
    extra = ({"channel", "session_id", "binding_generation", "delivery_id"} if website
             else {"provider_thread_id", "bot_id", "work_kind"})
    if (set(value) != common | extra
            or value.get("supplier_instance_id") != supplier
            or value.get("process_incarnation_id") != process
            or type(value.get("contract_version")) is not int
            or value["contract_version"] != (1 if website else 2)
            or not any(matches(row, value) for row in allowed)
            or (not website and value.get("work_kind") != "SEND_CANDIDATE")):
        raise AttestationUnavailable()
    # Reject malformed identity before retaining it or sending any producer request.
    for key in common | extra:
        if key.endswith("_id") and key not in {"owner_id", "bot_id", "provider_thread_id"}:
            if not isinstance(value[key], str) or str(UUID(value[key])) != value[key]:
                raise AttestationUnavailable()
    for key in ("outbound_ordinal", "binding_generation", "bot_id"):
        if key in value and (type(value[key]) is not int or value[key] < 1):
            raise AttestationUnavailable()
    if value["source_kind"] not in {"AGENT_TURN", "OUTBOUND_MESSAGE"}:
        raise AttestationUnavailable()


@dataclass
class Joined:
    value: object = None
    error: BaseException | None = None
    cancelled: bool = False


async def join_owned(awaitable):
    """Cancellation is remembered; the exact owned coroutine still gets joined."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return Joined(value=await asyncio.shield(task), cancelled=cancelled)
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                try:
                    return Joined(value=task.result(), cancelled=True)
                except BaseException as error:
                    return Joined(error=error, cancelled=True)
        except Exception as error:
            return Joined(error=error, cancelled=cancelled)


class WriterAttestor:
    """Local child capability: no producer/proof/retirement network credential."""
    def __init__(self):
        self.allowed = scopes()
        self.supplier = os.environ.get("WRITER_CHILD_SUPPLIER", "")
        self.process = os.environ.get("WRITER_CHILD_PROCESS", "")
        self.socket = os.environ.get("WRITER_CHILD_SOCKET", "")
        self.secret = os.environ.get("WRITER_CHILD_CAPABILITY", "")
        self.closed = set()

    def required(self, descriptor):
        # A pinned integration with a changed owner/agent is unavailable, not legacy.
        return any(row["integration_id"] == str(descriptor.get("integration_id"))
                   and row["channel"] == descriptor.get("channel", "TELEGRAM")
                   for row in self.allowed)

    def identity(self):
        if not all((self.socket, self.secret, self.supplier, self.process)):
            raise AttestationUnavailable()
        return UUID(self.supplier), UUID(self.process)

    async def _call(self, operation, payload):
        self.identity()
        connector = aiohttp.UnixConnector(path=self.socket)
        try:
            async with aiohttp.ClientSession(connector=connector,
                    timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.post("http://localhost/" + operation, json=payload,
                        headers={"X-Local-Capability": self.secret}) as response:
                    raw = await response.content.read(4097)
                    if response.status != 200 or len(raw) > 4096:
                        raise AttestationUnavailable()
                    receipt = json.loads(raw)
                    if (set(receipt) != {"contract_version", "registration_id", "state", "replayed"}
                            or type(receipt["contract_version"]) is not int
                            or receipt["contract_version"] != 1
                            or receipt["registration_id"] != payload["registration_id"]
                            or receipt["state"] not in {"OPEN", "CLOSED"}
                            or type(receipt["replayed"]) is not bool):
                        raise AttestationUnavailable()
                    return receipt
        except Exception:
            raise AttestationUnavailable() from None

    async def register(self, descriptor):
        validate_descriptor(descriptor, self.supplier, self.process, self.allowed)
        rid = registration_id(descriptor)
        if rid in self.closed:
            raise AttestationUnavailable()
        receipt = await self._call("register", {"registration_id": rid, "descriptor": descriptor})
        if receipt["state"] != "OPEN":
            self.closed.add(rid)
            raise AttestationUnavailable()
        return rid

    async def close(self, descriptor):
        rid = registration_id(descriptor)
        self.closed.add(rid)  # Irreversible local fence before IPC; never resubmit begin.
        while True:
            try:
                receipt = await self._call("joined", {"registration_id": rid})
                if receipt["state"] != "CLOSED":
                    raise AttestationUnavailable()
                return
            except AttestationUnavailable:
                # Keep ownership until parent durably closes or this child exits.
                await asyncio.sleep(0.25)


class Producer:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    async def post(self, route, payload):
        # No exception/body/URL is logged. Immutable payload is retried by parent.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(self.url + "/internal/v1/writer-attestations/" + route,
                    json=payload, allow_redirects=False,
                    headers={"X-Writer-Producer-Token": self.token}) as response:
                raw = await response.content.read(4097)
                if response.status != 200 or len(raw) > 4096:
                    raise AttestationUnavailable()
                ack = json.loads(raw)
                if (set(ack) != {"contract_version", "registration_id", "state", "replayed"}
                        or type(ack["contract_version"]) is not int or ack["contract_version"] != 1
                        or ack["registration_id"] != payload["registration_id"]
                        or ack["state"] not in {"OPEN", "CLOSED"}
                        or type(ack["replayed"]) is not bool
                        or (route == "witness" and ack["state"] != "CLOSED")):
                    raise AttestationUnavailable()
                return ack


class Parent:
    """One owned OS child. Wait completion is the only process-ended observation."""
    def __init__(self, producer, allowed):
        self.producer, self.allowed = producer, allowed
        self.supplier, self.process = str(uuid4()), str(uuid4())
        self.secret = secrets.token_urlsafe(48)
        self.slots = {}
        self.operations = set()
        self.accepting = True
        self.child = None
        self.waited = False

    async def retry(self, route, payload):
        while True:
            try:
                return await self.producer.post(route, payload)
            except Exception:
                await asyncio.sleep(0.25)

    async def register(self, payload):
        if not self.accepting or set(payload) != {"registration_id", "descriptor"}:
            raise AttestationUnavailable()
        descriptor = payload["descriptor"]
        validate_descriptor(descriptor, self.supplier, self.process, self.allowed)
        rid = registration_id(descriptor)
        if payload["registration_id"] != rid:
            raise AttestationUnavailable()
        slot = self.slots.get(rid)
        if slot is None:
            if len(self.slots) >= 4096:
                raise AttestationUnavailable()
            slot = {"registration": {"contract_version": 1, **payload}, "witness": None,
                    "lock": asyncio.Lock(), "registered": False, "closed": False}
            self.slots[rid] = slot
        async with slot["lock"]:
            ack = await self.retry("register", slot["registration"])
            slot["registered"] = True
            return ack

    async def close(self, rid, kind):
        if kind == "PROCESS_TERMINATED" and not self.waited:
            raise AttestationUnavailable()
        slot = self.slots.get(rid)
        if slot is None:
            raise AttestationUnavailable()
        async with slot["lock"]:
            if slot["witness"] is None:
                slot["witness"] = {"contract_version": 1, "registration_id": rid,
                    "proof_ref": str(uuid4()), "proof_kind": kind, "fenced": True,
                    "tasks_ended": True, "observed_at": datetime.now(timezone.utc).isoformat()}
            if not slot["registered"]:
                await self.retry("register", slot["registration"])
                slot["registered"] = True
            ack = await self.retry("witness", slot["witness"])
            slot["closed"] = True
            return ack

    async def handle(self, request):
        if (not self.accepting or not hmac.compare_digest(
                request.headers.get("X-Local-Capability", ""), self.secret)):
            return web.json_response({"error": "unavailable"}, status=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise AttestationUnavailable()
            if request.path == "/register":
                coroutine = self.register(payload)
            elif request.path == "/joined" and set(payload) == {"registration_id"}:
                coroutine = self.close(payload["registration_id"], "TASKS_FENCED_AND_JOINED")
            else:
                raise AttestationUnavailable()
            task = asyncio.create_task(coroutine)
            self.operations.add(task)
            task.add_done_callback(self.operations.discard)
            # Client timeout/disconnect cannot discard a retained observation.
            result = await asyncio.shield(task)
            return web.json_response(result)
        except Exception:
            return web.json_response({"error": "unavailable"}, status=400)

    def child_environment(self, socket_path, environment=None):
        env = dict(os.environ if environment is None else environment)
        # Drop all attestor network secrets/config. Parent never passes a token file.
        for key in list(env):
            if (key.startswith(("WRITER_PRODUCER_", "WRITER_SUPERVISOR_", "WRITER_PROOF_", "WRITER_RETIREMENT_")) or "PROOF_TOKEN" in key
                    or "SUPERVISOR_TOKEN" in key or "RETIREMENT_TOKEN" in key
                    or key == "WRITER_PRODUCERS"):
                env.pop(key)
        env.update(WRITER_CHILD_SUPPLIER=self.supplier, WRITER_CHILD_PROCESS=self.process,
                   WRITER_CHILD_SOCKET=str(socket_path), WRITER_CHILD_CAPABILITY=self.secret,
                   WRITER_CHILD_DRAIN_RECEIPT=str(Path(socket_path).parent / "drain.json"))
        return env

    async def run_child(self, command=None, *, environment=None, pass_fds=()):
        with tempfile.TemporaryDirectory(prefix="chota-attestor-") as directory:
            path = Path(directory) / "writer.sock"
            app = web.Application(client_max_size=16384)
            app.router.add_post("/register", self.handle)
            app.router.add_post("/joined", self.handle)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.UnixSite(runner, str(path)).start()
            try:
                self.child = await asyncio.create_subprocess_exec(
                    *(command or [sys.executable, "-m", "services.writer_child"]),
                    env=self.child_environment(path, environment), pass_fds=pass_fds)
                result = await self.child.wait()  # actual owned OS process handle/join
                self.waited = True
                self.accepting = False
                if self.operations:
                    await asyncio.gather(*tuple(self.operations), return_exceptions=True)
                for rid in tuple(self.slots):
                    await self.close(rid, "PROCESS_TERMINATED")
                return result
            finally:
                await runner.cleanup()


def deny_sender_subprocesses():
    # The approved transports send inline. A descendant could outlive waitpid,
    # so executable spawning/forking is forbidden in the gateway child.
    def audit(event, args):
        if (event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty",
                      "os.posix_spawn", "os.exec", "os.spawn"}):
            raise RuntimeError("gateway_sender_subprocess_forbidden")
    sys.addaudithook(audit)


async def serve():
    allowed = scopes()
    drains = scopes("WRITER_LEGACY_DRAIN_SCOPES")
    if any(not any(matches(row, scope) for row in drains) for scope in allowed):
        raise AttestationUnavailable()
    url, token = os.environ.get("WRITER_PRODUCER_URL", ""), os.environ.get("WRITER_PRODUCER_TOKEN", "")
    if allowed and (not url.startswith(("http://", "https://")) or not 32 <= len(token) <= 256):
        raise AttestationUnavailable()
    parent = Parent(Producer(url, token), allowed)
    stopping = False
    def stop():
        nonlocal stopping
        stopping = True
        if parent.child is not None and parent.child.returncode is None:
            parent.child.terminate()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)
    while not stopping:
        await parent.run_child()
        if not stopping:
            # run_child returns only after every old-child slot has durable closure.
            parent = Parent(Producer(url, token), allowed)
            await asyncio.sleep(0.25)


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except Exception:
        # Startup diagnostics must not include environment, payloads or credentials.
        print("writer_attestor_unavailable", file=sys.stderr)
        raise SystemExit(1) from None
