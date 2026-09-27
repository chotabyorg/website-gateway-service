from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.logger import logger


@dataclass
class IntegrationConfig:
    integration_id: str
    agent_id: str
    owner_id: str
    credentials: dict


@dataclass
class WidgetValidation:
    integration_id: str
    agent_id: str
    owner_id: str
    config: dict


class ControlPlaneClient:
    def __init__(self, base_url: str, internal_token: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=10.0,
            headers={"X-Internal-Token": internal_token},
        )

    async def fetch_integrations(self) -> list[IntegrationConfig]:
        try:
            response = await self.client.get(
                "/v1/internal/integrations",
                params={"channel_type": "WEBSITE_WIDGET"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch WEBSITE_WIDGET integrations from control-plane: %s", exc)
            raise

        items = response.json()
        configs: list[IntegrationConfig] = []
        for item in items:
            configs.append(
                IntegrationConfig(
                    integration_id=item["integration_id"],
                    agent_id=item["agent_id"],
                    owner_id=item.get("owner_id", ""),
                    credentials=item.get("credentials", {}),
                )
            )
        return configs

    async def validate_widget_key(self, api_key: str) -> WidgetValidation | None:
        """Validate a widget key against control-plane and return trusted ownership."""
        try:
            response = await self.client.get(
                "/v1/widget/internal/validate",
                params={"api_key": api_key},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Widget key validation request to control-plane failed: %s", exc)
            raise

        item = response.json()
        return WidgetValidation(
            integration_id=item["integration_id"],
            agent_id=item["agent_id"],
            owner_id=item["owner_id"],
            config=item.get("config", {}),
        )

    async def close(self) -> None:
        await self.client.aclose()
