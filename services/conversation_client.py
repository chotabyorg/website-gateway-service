"""Private Website admission/result/transport client. Never invokes legacy AI."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic
from uuid import NAMESPACE_URL, uuid5

import httpx

from core.logger import log_event
from core.routing import strict_uuid

_BASE = "/internal/v1/website"


def response_attempt_for(admission_id: str) -> str:
    """Return the stable v2 transport identity for one durable admission."""
    canonical_admission_id = strict_uuid(admission_id)
    return str(uuid5(
        NAMESPACE_URL,
        f"main-chota.website-widget-response.v2:{canonical_admission_id}",
    ))


class ConversationUnavailable(Exception):
    pass


class ConversationConflict(Exception):
    pass


@dataclass(frozen=True)
class WebsiteEnvelope:
    owner_id: str
    integration_id: str
    agent_id: str
    session_id: str
    message_id: str
    text: str

    def lookup(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in ("owner_id", "integration_id", "agent_id", "session_id", "message_id")}


@dataclass(frozen=True)
class WebsiteAdmission:
    route: str
    admission_id: str
    replayed: bool


@dataclass(frozen=True)
class WebsiteResponseIntent:
    response_attempt_id: str
    draft_text: str
    action: str
    send: bool
    replayed: bool
    replay_source_attempt_id: str | None = None
    denial_reason: str | None = None


class ConversationClient:
    def __init__(self, base_url: str, token: str, *, request_timeout: float = 10,
                 result_wait: float = 120, poll_interval: float = 0.5,
                 poll_initial: float = 0.1) -> None:
        self.client = httpx.AsyncClient(base_url=base_url, timeout=request_timeout,
                                       headers={"X-Service-Token": token})
        self.result_wait = result_wait
        self.poll_interval = poll_interval
        self.poll_initial = min(poll_initial, poll_interval)

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            response = await self.client.post(_BASE + path, json=payload)
            log_event(logging.INFO, "conversation_response", status=response.status_code)
            if response.status_code == 409:
                raise ConversationConflict("managed_message_conflict")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("invalid_conversation_response")
            return body
        except ConversationConflict:
            raise
        except Exception as exc:
            log_event(logging.ERROR, "conversation_unavailable", exc=exc)
            raise ConversationUnavailable("managed_service_unavailable") from None

    async def admit(self, envelope: WebsiteEnvelope, desired_route: str) -> WebsiteAdmission:
        data = await self._post("/admissions", {**envelope.lookup(), "text": envelope.text,
                                                "desired_route": desired_route})
        route = data.get("route")
        admission_id = data.get("admission_id")
        replayed = data.get("replayed")
        if (route not in ("MANAGED", "LEGACY")
                or type(replayed) is not bool):
            raise ConversationUnavailable("invalid_admission_disposition")
        try:
            canonical_admission_id = strict_uuid(admission_id)
        except ValueError:
            raise ConversationUnavailable("invalid_admission_disposition") from None
        return WebsiteAdmission(route, canonical_admission_id, replayed)

    async def wait_result(self, envelope: WebsiteEnvelope) -> dict:
        deadline = monotonic() + self.result_wait
        # Start responsive, then back off to the configured ceiling: short
        # answers are fetched immediately while a long managed turn no longer
        # produces a request every few hundred milliseconds for minutes.
        delay = self.poll_initial
        while True:
            data = await self._post("/results", envelope.lookup())
            state = data.get("state")
            if state not in ("PENDING", "DRAFT_READY", "NO_RESPONSE", "FAILED", "UNKNOWN"):
                raise ConversationUnavailable("invalid_result_state")
            if state == "DRAFT_READY" and not isinstance(data.get("draft_text"), str):
                raise ConversationUnavailable("invalid_retained_draft")
            if state != "PENDING" or monotonic() >= deadline:
                return data
            await asyncio.sleep(min(delay, max(0, deadline - monotonic())))
            delay = min(self.poll_interval, max(self.poll_initial, delay * 2))

    async def begin_response(
        self, envelope: WebsiteEnvelope, response_attempt_id: str
    ) -> WebsiteResponseIntent:
        data = await self._post("/responses/begin", {**envelope.lookup(), "response_attempt_id": response_attempt_id})
        action = data.get("action")
        replay_source_attempt_id = data.get("replay_source_attempt_id")
        denial_reason = data.get("denial_reason")
        if (data.get("response_attempt_id") != response_attempt_id
                or not isinstance(data.get("draft_text"), str)
                or type(data.get("send")) is not bool
                or type(data.get("replayed")) is not bool
                or denial_reason not in {None, "SUPERSEDED", "DELIVERY_NOT_RETRYABLE"}
                or action not in {"SEND", "REPLAY_COMPLETED", "DENY"}):
            raise ConversationUnavailable("invalid_response_intent")

        if replay_source_attempt_id is not None:
            try:
                replay_source_attempt_id = strict_uuid(replay_source_attempt_id)
            except ValueError:
                raise ConversationUnavailable("invalid_response_intent") from None

        valid = (
            (action == "SEND" and data["send"] and not data["replayed"]
             and replay_source_attempt_id is None and denial_reason is None)
            or (action == "REPLAY_COMPLETED" and not data["send"] and data["replayed"]
                and replay_source_attempt_id is not None and denial_reason is None)
            or (action == "DENY" and not data["send"] and replay_source_attempt_id is None
                and denial_reason is not None)
        )
        if not valid:
            raise ConversationUnavailable("invalid_response_intent")
        return WebsiteResponseIntent(
            response_attempt_id=response_attempt_id,
            draft_text=data["draft_text"],
            action=action,
            send=data["send"],
            replayed=data["replayed"],
            replay_source_attempt_id=replay_source_attempt_id,
            denial_reason=denial_reason,
        )

    async def complete_response(self, envelope: WebsiteEnvelope, response_attempt_id: str, outcome: str) -> None:
        data = await self._post("/responses/complete", {**envelope.lookup(), "response_attempt_id": response_attempt_id,
                                                       "outcome": outcome})
        if data.get("response_attempt_id") != response_attempt_id or data.get("outcome") != outcome:
            raise ConversationUnavailable("invalid_transport_evidence")

    async def close(self) -> None:
        await self.client.aclose()
