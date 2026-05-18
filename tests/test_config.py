def test_ai_agent_request_timeout_default(load_module):
    config = load_module("core.config")

    assert config.settings.AI_AGENT_REQUEST_TIMEOUT_SECONDS == 120
