"""Private Website admission/result/transport client. Never invokes legacy AI."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

import httpx

from core.logger import log_event

_BASE = "/internal/v1/website"


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


class ConversationClient:
    def __init__(self, base_url: str, token: str, *, request_timeout: float = 10,
                 result_wait: float = 120, poll_interval: float = 0.25) -> None:
        self.client = httpx.AsyncClient(base_url=base_url, timeout=request_timeout,
                                       headers={"X-Service-Token": token})
        self.result_wait = result_wait
        self.poll_interval = poll_interval

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

    async def admit(self, envelope: WebsiteEnvelope, desired_route: str) -> str:
        data = await self._post("/admissions", {**envelope.lookup(), "text": envelope.text,
                                                "desired_route": desired_route})
        route = data.get("route")
        if route not in ("MANAGED", "LEGACY"):
            raise ConversationUnavailable("invalid_admission_disposition")
        return route

    async def wait_result(self, envelope: WebsiteEnvelope) -> dict:
        deadline = monotonic() + self.result_wait
        while True:
            data = await self._post("/results", envelope.lookup())
            state = data.get("state")
            if state not in ("PENDING", "DRAFT_READY", "NO_RESPONSE", "FAILED", "UNKNOWN"):
                raise ConversationUnavailable("invalid_result_state")
            if state == "DRAFT_READY" and not isinstance(data.get("draft_text"), str):
                raise ConversationUnavailable("invalid_retained_draft")
            if state != "PENDING" or monotonic() >= deadline:
                return data
            await asyncio.sleep(min(self.poll_interval, max(0, deadline - monotonic())))

    async def begin_response(self, envelope: WebsiteEnvelope, response_attempt_id: str) -> str:
        data = await self._post("/responses/begin", {**envelope.lookup(), "response_attempt_id": response_attempt_id})
        if data.get("response_attempt_id") != response_attempt_id or not isinstance(data.get("draft_text"), str):
            raise ConversationUnavailable("invalid_response_intent")
        return data["draft_text"]

    async def complete_response(self, envelope: WebsiteEnvelope, response_attempt_id: str, outcome: str) -> None:
        data = await self._post("/responses/complete", {**envelope.lookup(), "response_attempt_id": response_attempt_id,
                                                       "outcome": outcome})
        if data.get("response_attempt_id") != response_attempt_id or data.get("outcome") != outcome:
            raise ConversationUnavailable("invalid_transport_evidence")

    async def close(self) -> None:
        await self.client.aclose()
