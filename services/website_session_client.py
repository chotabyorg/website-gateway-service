"""Verified, bounded private Website client; no retry or public transport authority."""
from __future__ import annotations

import json
import re
import ssl
from typing import TypeVar
from urllib.parse import urlsplit

import httpx

from services.website_session_contracts import (
    Contract,
    WebsiteAdmissionReceipt,
    WebsiteDeliveryBegin,
    WebsiteDeliveryBegun,
    WebsiteDeliveryComplete,
    WebsiteDeliveryCompleted,
    WebsiteDeliveryDescriptor,
    WebsiteDeliveryWorkResult,
    WebsiteHistoryPage,
    WebsiteHistoryRequest,
    WebsitePollAccess,
    WebsitePollBegin,
    WebsitePollEnded,
    WebsitePollStarted,
    WebsiteSessionAccess,
    WebsiteSessionAdmission,
    WebsiteSessionCreated,
    WebsiteSessionCreate,
)

Result = TypeVar("Result", bound=Contract)
BASE = "/internal/v1/website/sessions"


class WebsiteSessionError(Exception):
    def __init__(self, status: int = 503, retry_after: int | None = None):
        self.status, self.retry_after = status, retry_after
        super().__init__("website_session_unavailable")


def _closed_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protocol field")
        result[key] = value
    return result


class WebsiteSessionClient:
    def __init__(self, base_url: str, token: str, *, expected_peer: str,
                 trusted_ca_file: str | None = None, request_timeout: float = 10,
                 transport: httpx.AsyncBaseTransport | None = None):
        peer = urlsplit(base_url)
        if (peer.scheme != "https" or not peer.hostname or peer.hostname != expected_peer
                or peer.username is not None or peer.password is not None
                or peer.path not in ("", "/") or peer.query or peer.fragment
                or not token or not 0.1 <= request_timeout <= 30):
            raise ValueError("invalid_verified_conversation_peer")
        tls = ssl.create_default_context(cafile=trusted_ca_file)
        self.client = httpx.AsyncClient(base_url=base_url, timeout=request_timeout,
            headers={"X-Service-Token": token}, verify=tls, trust_env=False,
            follow_redirects=False, transport=transport)

    async def _post(self, suffix: str, request: Contract,
                    response_type: type[Result], maximum: int = 65536) -> Result:
        payload = request.model_dump(mode="json")
        if isinstance(request, WebsiteSessionAccess):
            raw = request.session_token.get_secret_value()
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", raw) is None:
                raise WebsiteSessionError(404)
            payload["session_token"] = raw
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 65536:
            raise WebsiteSessionError(413)
        try:
            async with self.client.stream("POST", BASE + suffix, content=encoded,
                    headers={"Content-Type": "application/json"}) as response:
                if response.status_code != 200 and not (suffix == "" and response.status_code == 201):
                    status = response.status_code
                    safe = status if status in {404, 409, 413, 422, 429} else 503
                    delay = response.headers.get("Retry-After", "")
                    retry = max(1, min(60, int(delay))) if delay.isascii() and delay.isdigit() else 1
                    raise WebsiteSessionError(safe, retry if safe == 429 else None)
                if response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise WebsiteSessionError()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > maximum:
                        raise WebsiteSessionError()
                    data.extend(chunk)
                value = json.loads(data, object_pairs_hook=_closed_object)
                parsed = response_type.model_validate(value)
                metadata_only = (isinstance(parsed, WebsiteDeliveryWorkResult)
                    and parsed.frame is None) or (isinstance(parsed, WebsiteDeliveryBegun)
                    and not parsed.send)
                if metadata_only and len(data) > 4096:
                    raise WebsiteSessionError()
                return parsed
        except WebsiteSessionError:
            raise
        except Exception:
            # No URL/body, credential-bearing validation dump, exception repr or
            # automatic retry. A lost begin reply is unresolved original authority.
            raise WebsiteSessionError() from None

    async def create(self, request: WebsiteSessionCreate) -> WebsiteSessionCreated:
        value = await self._post("", request, WebsiteSessionCreated, 4096)
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", value.session_token.get_secret_value()) is None:
            raise WebsiteSessionError()
        return value

    async def admit(self, request: WebsiteSessionAdmission) -> WebsiteAdmissionReceipt:
        return await self._post("/admissions", request, WebsiteAdmissionReceipt, 4096)

    async def history(self, request: WebsiteHistoryRequest) -> WebsiteHistoryPage:
        return await self._post("/history", request, WebsiteHistoryPage, 262144)

    async def begin_poll(self, request: WebsitePollBegin) -> WebsitePollStarted:
        return await self._post("/polls/begin", request, WebsitePollStarted, 4096)

    async def end_poll(self, request: WebsitePollAccess) -> WebsitePollEnded:
        return await self._post("/polls/end", request, WebsitePollEnded, 4096)

    async def work(self, request: WebsitePollAccess) -> WebsiteDeliveryWorkResult:
        value = await self._post("/delivery/work", request, WebsiteDeliveryWorkResult)
        if value.item is not None:
            if ((value.frame is not None and value.frame.delivery_id != value.item.delivery_id)
                    or (value.recovery is not None
                        and value.recovery.delivery_id != value.item.delivery_id)):
                raise WebsiteSessionError()
            if value.frame is not None and value.frame.actor != (
                    "AGENT" if value.item.source_kind == "AGENT_TURN" else "HUMAN"):
                raise WebsiteSessionError()
        if value.state == "RECOVER_ONLY" and len(value.model_dump_json().encode("utf-8")) > 4096:
            raise WebsiteSessionError()
        return value

    async def begin(self, request: WebsiteDeliveryBegin) -> WebsiteDeliveryBegun:
        value = await self._post("/delivery/begin", request, WebsiteDeliveryBegun)
        expected = WebsiteDeliveryDescriptor.model_validate({
            key: getattr(request, key) for key in WebsiteDeliveryDescriptor.model_fields})
        if (value.item != expected or value.recovery.delivery_id != request.delivery_id
                or (value.frame is not None and value.frame.delivery_id != request.delivery_id)
                or (request.recover_only and value.send)
                or (value.send and (value.replayed or value.recovery.writer_state != "LIVE"
                    or value.recovery.status != "WRITE_IN_PROGRESS"
                    or value.frame.actor != (
                        "AGENT" if request.source_kind == "AGENT_TURN" else "HUMAN")))):
            raise WebsiteSessionError()
        if not value.send and len(value.model_dump_json().encode("utf-8")) > 4096:
            raise WebsiteSessionError()
        return value

    async def complete(self, request: WebsiteDeliveryComplete) -> WebsiteDeliveryCompleted:
        value = await self._post("/delivery/complete", request, WebsiteDeliveryCompleted, 4096)
        if value.recovery.delivery_id != request.delivery_id:
            raise WebsiteSessionError()
        return value

    async def close(self):
        await self.client.aclose()
