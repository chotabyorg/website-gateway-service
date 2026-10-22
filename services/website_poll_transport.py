"""One owned HTTP writer per durable poll; no retry after uncertain acquisition."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from uuid import uuid4

from aiohttp import web

from api.middleware import apply_response_headers
from services.website_session_client import WebsiteSessionError
from services.website_session_contracts import (
    WebsiteDeliveryBegin, WebsiteDeliveryComplete, WebsitePollAccess,
    WebsitePollBegin, WebsiteSessionAccess,
)


@dataclass
class Joined:
    value: object = None
    error: BaseException | None = None
    cancelled: bool = False


async def owned(awaitable, timeout: float) -> Joined:
    """Shield, bound and actually join the original task before returning evidence."""
    task = asyncio.create_task(asyncio.wait_for(awaitable, timeout=max(0.001, timeout)))
    interrupted = False
    while True:
        try:
            return Joined(value=await asyncio.shield(task), cancelled=interrupted)
        except asyncio.CancelledError:
            interrupted = True
            if task.done():
                try:
                    return Joined(value=task.result(), cancelled=True)
                except BaseException as error:
                    return Joined(error=error, cancelled=True)
        except Exception as error:
            return Joined(error=error, cancelled=interrupted)


def encoded(payload, maximum=4096) -> bytes:
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(value) > maximum:
        raise ValueError("public_frame_too_large")
    return value


def metadata(value):
    result = {"state": value.state}
    if value.item is not None:
        result["delivery_id"] = str(value.item.delivery_id)
    if value.recovery is not None:
        result.update(status=value.recovery.status, writer_state=value.recovery.writer_state)
    return result


def public_frame(frame):
    return encoded({"state": "DELIVERY", "delivery": frame.model_dump(mode="json")}, 65536)


class WebsitePollTransport:
    def __init__(self, client, *, wait_seconds=20.0, poll_interval=0.25,
                 call_timeout=10.0, cleanup_timeout=5.0):
        if (not 0.05 <= wait_seconds <= 20 or not 0.01 <= poll_interval <= 1
                or not 0.01 <= call_timeout <= 30 or not 0.01 <= cleanup_timeout <= 10):
            raise ValueError("invalid_poll_transport_limits")
        self.client = client
        self.wait_seconds, self.poll_interval = wait_seconds, poll_interval
        self.call_timeout, self.cleanup_timeout = call_timeout, cleanup_timeout
        # These are original writer identities, not a host/process attestation.
        # A replacement process must get fresh identities and cannot join its predecessor.
        self.supplier_instance_id, self.process_incarnation_id = uuid4(), uuid4()

    async def _write(self, request, response):
        apply_response_headers(request, response)
        await response.prepare(request)
        await response.write_eof()

    def _response(self, payload, *, status=200, raw=False, retry_after=None):
        headers = {"Cache-Control": "no-store"}
        if retry_after is not None:
            headers["Retry-After"] = str(max(1, min(60, retry_after)))
        return web.Response(body=payload if raw else encoded(payload), status=status,
                            content_type="application/json", headers=headers)

    async def run(self, request, access: WebsiteSessionAccess, *, deadline=None):
        loop = asyncio.get_running_loop()
        deadline = min(deadline or loop.time() + self.wait_seconds,
                       loop.time() + self.wait_seconds)
        cancelled = False
        reservation = None
        response = None

        async def joined(awaitable, timeout):
            nonlocal cancelled
            result = await owned(awaitable, timeout)
            cancelled = cancelled or result.cancelled
            return result

        async def finish(payload, *, raw=False, status=200, retry_after=None):
            nonlocal response
            response = self._response(payload, raw=raw, status=status, retry_after=retry_after)
            remaining = deadline - loop.time()
            if cancelled or remaining <= 0:
                if request.transport is not None:
                    request.transport.close()
                return
            write = await joined(self._write(request, response), remaining)
            # Closing a timed-out/failed metadata response bounds the public poll.
            if write.error is not None and request.transport is not None:
                request.transport.close()

        async def completion(begin, outcome, error_code=None):
            # Both the original begin task and its only possible write task have
            # returned. No detached callback/future can subsequently write text.
            command = WebsiteDeliveryComplete(**begin.model_dump(exclude={"poll_id"}),
                outcome=outcome, error_code=error_code, writer_end={"kind": "TASK_JOINED",
                    "supplier_instance_id": self.supplier_instance_id,
                    "process_incarnation_id": self.process_incarnation_id})
            return await joined(self.client.complete(command), self.cleanup_timeout)

        try:
            granted = await joined(self.client.begin_poll(WebsitePollBegin(**access.model_dump(),
                request_id=uuid4(), supplier_instance_id=self.supplier_instance_id,
                process_incarnation_id=self.process_incarnation_id)),
                min(self.call_timeout, max(0.001, deadline - loop.time())))
            if granted.error is not None:
                raise granted.error
            if granted.value.state != "ACQUIRED":
                await finish({"state": "RECOVER_ONLY"})
            else:
                reservation = WebsitePollAccess(**access.model_dump(), poll_id=granted.value.poll_id,
                    supplier_instance_id=self.supplier_instance_id,
                    process_incarnation_id=self.process_incarnation_id)
                while not cancelled and loop.time() < deadline - 0.05:
                    # Reserve a small final-write budget inside the 20s request bound.
                    work = await joined(self.client.work(reservation),
                        min(self.call_timeout, max(0.001, deadline - loop.time() - 0.05)))
                    if work.error is not None:
                        raise work.error
                    if cancelled:
                        break
                    candidate = work.value
                    if candidate.state == "EMPTY":
                        await joined(asyncio.sleep(min(self.poll_interval,
                            max(0.001, deadline - loop.time() - 0.05))),
                            max(0.001, deadline - loop.time()))
                        continue
                    if candidate.state != "SEND_CANDIDATE":
                        await finish(metadata(candidate))
                        break
                    # Size the complete public JSON before acquiring any writer.
                    try:
                        public_frame(candidate.frame)
                    except ValueError:
                        await finish({"state": "FRAME_TOO_LARGE",
                                      "delivery_id": str(candidate.item.delivery_id)})
                        break
                    begin = WebsiteDeliveryBegin(**reservation.model_dump(),
                        **candidate.item.model_dump(), recover_only=False)
                    acquired = await joined(self.client.begin(begin),
                        min(self.call_timeout, max(0.001, deadline - loop.time() - 0.05)))
                    if acquired.error is not None:
                        # Lost ACK never authorizes a write or a second begin.
                        # Exact original identity permits UNKNOWN/join reconciliation
                        # if the acquisition committed before its response was lost.
                        await finish({"state": "RECOVER_ONLY",
                                      "delivery_id": str(begin.delivery_id), "status": "UNKNOWN"})
                        await completion(begin, "UNKNOWN", "TIMEOUT" if isinstance(
                            acquired.error, TimeoutError) else "TRANSPORT_ERROR")
                        break
                    decision = acquired.value
                    if not decision.send:
                        await finish(metadata(decision))
                        break
                    # Never expose discovery text. Only this positive original
                    # acquisition's validated frame can reach the owned socket task.
                    outcome, error_code = "UNKNOWN", "TRANSPORT_ERROR"
                    try:
                        if (decision.state != "ACQUIRED" or decision.replayed
                                or decision.item != candidate.item
                                or decision.frame != candidate.frame):
                            raise ValueError("invalid_original_acquisition")
                        raw = public_frame(decision.frame)
                        if not cancelled and loop.time() < deadline:
                            response = self._response(raw, raw=True)
                            write = await joined(self._write(request, response), deadline - loop.time())
                            if write.error is None:
                                outcome, error_code = "WRITE_COMPLETED", None
                            elif isinstance(write.error, TimeoutError):
                                error_code = "TIMEOUT"
                            if write.error is not None and request.transport is not None:
                                request.transport.close()
                        else:
                            if request.transport is not None:
                                request.transport.close()
                    except (ValueError, TypeError):
                        await finish({"state": "RECOVER_ONLY",
                                      "delivery_id": str(begin.delivery_id), "status": "UNKNOWN"})
                    await completion(begin, outcome, error_code)
                    break
                if response is None:
                    await finish({"state": "EMPTY"})
        except WebsiteSessionError as error:
            await finish({"error": "Session unavailable"}, status=error.status,
                         retry_after=error.retry_after)
        except TimeoutError:
            await finish({"state": "EMPTY"})
        finally:
            if reservation is not None:
                # Resource cleanup is independent of writer completion. Its failure,
                # expiry or replacement can never end/retire a DeliveryAttempt.
                await joined(self.client.end_poll(reservation), self.cleanup_timeout)
        if cancelled:
            raise asyncio.CancelledError
        return response
