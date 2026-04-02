import httpx

from core.logger import logger


class CoreAPIClient:
    def __init__(self, base_url: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
        )

    async def send_message(self, agent_id: str, session_id: str, message: str) -> str:
        payload = {
            "agent_id": agent_id,
            "external_client_id": session_id,
            "channel": "website_widget",
            "message": message,
        }
        try:
            response = await self.client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            logger.info("AI response received for session_id=%s", data.get("session_id"))
            return str(data.get("answer", ""))
        except httpx.HTTPError as exc:
            logger.error("Core API error: %s", exc)
            return "Извините, сервис временно недоступен. Попробуйте позже."

    async def close(self) -> None:
        await self.client.aclose()
