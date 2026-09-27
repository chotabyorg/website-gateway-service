from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.logger import logger
from services.control_plane_client import ControlPlaneClient


@dataclass
class WidgetInfo:
    integration_id: str
    agent_id: str
    owner_id: str
    widget_api_key: str
    config: dict


class WidgetKeyManager:
    def __init__(self, cp_client: ControlPlaneClient) -> None:
        self._cp_client = cp_client
        self._widgets: dict[str, WidgetInfo] = {}
        self._lock = asyncio.Lock()

    async def reconcile(self) -> None:
        try:
            integrations = await self._cp_client.fetch_integrations()
        except Exception as exc:
            logger.error("Reconcile failed, preserving existing cache: %s", exc)
            return

        async with self._lock:
            new_keys: set[str] = set()
            for integration in integrations:
                api_key = integration.credentials.get("widget_api_key", "")
                if not api_key:
                    continue
                new_keys.add(api_key)
                self._widgets[api_key] = WidgetInfo(
                    integration_id=integration.integration_id,
                    agent_id=integration.agent_id,
                    owner_id=integration.owner_id,
                    widget_api_key=api_key,
                    config=integration.credentials.get("widget_config", {}),
                )

            removed = set(self._widgets.keys()) - new_keys
            for key in removed:
                del self._widgets[key]

        logger.info(
            "Reconcile complete: %d active widgets, %d removed",
            len(new_keys),
            len(removed),
        )

    def validate_key(self, api_key: str) -> WidgetInfo | None:
        return self._widgets.get(api_key)

    async def validate_key_for_chat(self, api_key: str) -> WidgetInfo | None:
        """Use control-plane's authenticated validation response for chat ownership."""
        validated = await self._cp_client.validate_widget_key(api_key)
        if validated is None:
            return None
        return WidgetInfo(
            integration_id=validated.integration_id,
            agent_id=validated.agent_id,
            owner_id=validated.owner_id,
            widget_api_key=api_key,
            config=validated.config,
        )

    def active_count(self) -> int:
        return len(self._widgets)
