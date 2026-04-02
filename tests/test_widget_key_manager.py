import pytest
from unittest.mock import AsyncMock

from services.control_plane_client import IntegrationConfig
from services.widget_key_manager import WidgetKeyManager


def _make_integration(
    integration_id: str = "int-1",
    agent_id: str = "agent-1",
    owner_id: str = "owner-1",
    api_key: str = "wk_abc",
    config: dict | None = None,
) -> IntegrationConfig:
    return IntegrationConfig(
        integration_id=integration_id,
        agent_id=agent_id,
        owner_id=owner_id,
        credentials={"widget_api_key": api_key, "widget_config": config or {}},
    )


@pytest.mark.asyncio
async def test_reconcile_adds_widgets():
    cp = AsyncMock()
    cp.fetch_integrations.return_value = [_make_integration(api_key="wk_abc")]

    manager = WidgetKeyManager(cp)
    await manager.reconcile()

    info = manager.validate_key("wk_abc")
    assert info is not None
    assert info.agent_id == "agent-1"
    assert info.integration_id == "int-1"


@pytest.mark.asyncio
async def test_reconcile_removes_deactivated():
    cp = AsyncMock()
    cp.fetch_integrations.return_value = [_make_integration(api_key="wk_active")]

    manager = WidgetKeyManager(cp)
    await manager.reconcile()

    # Remove the integration on next reconcile
    cp.fetch_integrations.return_value = []
    await manager.reconcile()

    assert manager.validate_key("wk_active") is None


@pytest.mark.asyncio
async def test_validate_key_returns_none_for_unknown():
    cp = AsyncMock()
    cp.fetch_integrations.return_value = []

    manager = WidgetKeyManager(cp)
    await manager.reconcile()

    assert manager.validate_key("wk_unknown") is None


@pytest.mark.asyncio
async def test_reconcile_updates_config():
    cp = AsyncMock()
    cp.fetch_integrations.return_value = [
        _make_integration(api_key="wk_abc", config={"theme": "light"})
    ]

    manager = WidgetKeyManager(cp)
    await manager.reconcile()

    assert manager.validate_key("wk_abc").config == {"theme": "light"}

    # Update config
    cp.fetch_integrations.return_value = [
        _make_integration(api_key="wk_abc", config={"theme": "dark"})
    ]
    await manager.reconcile()

    assert manager.validate_key("wk_abc").config == {"theme": "dark"}


@pytest.mark.asyncio
async def test_reconcile_preserves_cache_on_error():
    cp = AsyncMock()
    cp.fetch_integrations.return_value = [_make_integration(api_key="wk_abc")]

    manager = WidgetKeyManager(cp)
    await manager.reconcile()

    # Simulate error
    cp.fetch_integrations.side_effect = Exception("network error")
    await manager.reconcile()  # Should not raise

    # Cache preserved
    assert manager.validate_key("wk_abc") is not None
