from time import perf_counter

import httpx

from core.config import settings
from core.logger import logger
from services.text_formatter import format_agent_reply_for_website


TIMEOUT_FALLBACK_REPLY = "Ответ занимает слишком много времени. Попробуйте еще раз чуть позже."
GENERIC_FALLBACK_REPLY = "Извините, сервис временно недоступен. Попробуйте позже."


class CoreAPIClient:
    def __init__(self, base_url: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(
                connect=5.0,
                read=settings.AI_AGENT_REQUEST_TIMEOUT_SECONDS,
                write=30.0,
                pool=10.0,
            ),
        )

    async def send_message(self, agent_id: str, session_id: str, message: str) -> str:
        payload = {
            "agent_id": agent_id,
            "external_client_id": session_id,
            "channel": "website_widget",
            "message": message,
        }
        started_at = perf_counter()
        try:
            response = await self.client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            elapsed = perf_counter() - started_at
            logger.info(
                "AI response received: elapsed=%.3fs agent_id=%s session_id=%s status=%s",
                elapsed,
                agent_id,
                session_id,
                response.status_code,
            )
            return format_agent_reply_for_website(str(data.get("answer", "")))
        except httpx.TimeoutException as exc:
            elapsed = perf_counter() - started_at
            logger.error(
                "Core API timeout: elapsed=%.3fs timeout_seconds=%.3f agent_id=%s session_id=%s error=%s",
                elapsed,
                settings.AI_AGENT_REQUEST_TIMEOUT_SECONDS,
                agent_id,
                session_id,
                exc,
            )
            return TIMEOUT_FALLBACK_REPLY
        except httpx.HTTPError as exc:
            elapsed = perf_counter() - started_at
            logger.error(
                "Core API error: elapsed=%.3fs agent_id=%s session_id=%s error_type=%s error=%s",
                elapsed,
                agent_id,
                session_id,
                type(exc).__name__,
                exc,
            )
            return GENERIC_FALLBACK_REPLY

    async def close(self) -> None:
        await self.client.aclose()
