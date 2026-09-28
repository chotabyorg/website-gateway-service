import importlib
import sys

import pytest


@pytest.fixture
def load_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://control-plane:55580")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-internal-token")
    monkeypatch.setenv("INTEGRATIONS_POLL_INTERVAL", "60")
    monkeypatch.setenv("AI_AGENT_SERVICE_URL", "http://ai-agent-service:55570")
    monkeypatch.setenv("CHAT_S2S_TOKEN", "test-chat-token")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    def _load(module_name: str):
        for name in list(sys.modules.keys()):
            if name.startswith(("core.", "services.", "api.")):
                sys.modules.pop(name, None)
        sys.modules.pop("core.config", None)
        return importlib.import_module(module_name)

    return _load
