from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from core.logger import log_event
from services.control_plane_client import ControlPlaneClient


@dataclass
class WidgetInfo:
    integration_id: str
    agent_id: str
    owner_id: str
    widget_api_key: str
    config: dict
    conversation_required: bool = False
    binding_generation: int = 1


class WidgetKeyManager:
    def __init__(self, cp_client: ControlPlaneClient) -> None:
        self._cp_client = cp_client
        self._widgets: dict[str, WidgetInfo] = {}
        self._lock = asyncio.Lock()

    async def reconcile(self) -> None:
        try:
            integrations = await self._cp_client.fetch_integrations()
            snapshot: dict[str, WidgetInfo] = {}
            for integration in integrations:
                api_key = integration.credentials.get("widget_api_key", "")
                if not api_key:
                    continue
                snapshot[api_key] = WidgetInfo(
                    integration_id=integration.integration_id,
                    agent_id=integration.agent_id,
                    owner_id=integration.owner_id,
                    widget_api_key=api_key,
                    config=integration.credentials.get("widget_config", {}),
                )
            async with self._lock:
                self._widgets = snapshot
        except Exception as exc:
            # Includes malformed decrypted config, without exception input/trace.
            log_event(logging.ERROR, "widget_reconcile_unavailable", exc=exc)
            return
        log_event(logging.INFO, "widget_reconcile_complete")

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
            conversation_required=validated.conversation_required,
            binding_generation=validated.binding_generation,
        )

    def active_count(self) -> int:
        return len(self._widgets)
