import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.config import Settings
from core.routing import MAX_INTEGRATIONS, MAX_SESSIONS, RoutingPolicy

INTEGRATION = "8d4a1b73-7973-4dcc-a3d4-1312d871c1d4"
AGENT = "0b750714-af8d-4d8c-8fdc-4ddfeda98af9"
SESSION = "22222222-2222-4222-8222-222222222222"


def raw(mode="SESSION_ALLOWLIST", sessions=None):
    return json.dumps({INTEGRATION: {"expected_agent_id": AGENT, "mode": mode,
                                    "session_ids": sessions if sessions is not None else [SESSION]}})


def test_empty_routing_is_legacy():
    assert not RoutingPolicy.parse().selected(INTEGRATION, SESSION)


def test_strict_session_allowlist_and_unrelated_integration():
    policy = RoutingPolicy.parse(json.dumps({INTEGRATION: AGENT}), raw())
    assert policy.selected(INTEGRATION, SESSION)
    assert not policy.selected(INTEGRATION, str(uuid4()))
    assert not policy.selected(str(uuid4()), SESSION)
    assert not policy.selected(INTEGRATION, [SESSION])


def test_integration_all_is_exact_and_requires_no_sessions():
    policy = RoutingPolicy.parse(json.dumps({INTEGRATION: AGENT}), raw("INTEGRATION_ALL", []))
    assert policy.selected(INTEGRATION, str(uuid4()))
    assert policy.selected(INTEGRATION, "opaque")  # Handler rejects managed non-UUID.
    assert not policy.selected(str(uuid4()), SESSION)


@pytest.mark.parametrize("guards,cohorts", [
    ('{"*":"' + AGENT + '"}', '{}'),
    ('{"' + INTEGRATION.upper() + '":"' + AGENT + '"}', '{}'),
    ('{"' + INTEGRATION + '":"not-uuid"}', '{}'),
    ('{"' + INTEGRATION + '":"' + AGENT + '","' + INTEGRATION + '":"' + AGENT + '"}', '{}'),
    ('[]', '{}'), ('{}', raw()),
    (json.dumps({INTEGRATION: AGENT}), raw("ALL")),
    (json.dumps({INTEGRATION: AGENT}), raw("SESSION_ALLOWLIST", [])),
    (json.dumps({INTEGRATION: AGENT}), raw("SESSION_ALLOWLIST", [SESSION, SESSION])),
    (json.dumps({INTEGRATION: AGENT}), raw("SESSION_ALLOWLIST", ["*"])),
    (json.dumps({INTEGRATION: AGENT}), raw("INTEGRATION_ALL")),
    (json.dumps({INTEGRATION: AGENT}), raw("SESSION_ALLOWLIST", [str(uuid4()) for _ in range(MAX_SESSIONS + 1)])),
    (json.dumps({str(uuid4()): AGENT for _ in range(MAX_INTEGRATIONS + 1)}), '{}'),
    (json.dumps({INTEGRATION: AGENT}), raw().replace('"mode":', '"unknown":')),
])
def test_routing_rejects_unsafe_policy(guards, cohorts):
    with pytest.raises(ValueError):
        RoutingPolicy.parse(guards, cohorts)


def test_guard_requires_dedicated_conversation_config(monkeypatch):
    monkeypatch.setenv("MANAGED_WEBSITE_GUARDED_INTEGRATIONS", json.dumps({INTEGRATION: AGENT}))
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    config = Settings(_env_file=None, CONVERSATION_SERVICE_URL="http://conversation-service:8000",
                      CONVERSATION_S2S_TOKEN="fake-dedicated-token")
    assert config.routing_policy().guards == {INTEGRATION: AGENT}
    assert "fake-dedicated-token" not in repr(config)


@pytest.mark.parametrize("name,value", [("CONVERSATION_RESULT_WAIT_SECONDS", 121),
                                        ("CONVERSATION_RESULT_POLL_SECONDS", 0),
                                        ("CONVERSATION_REQUEST_TIMEOUT_SECONDS", 31)])
def test_conversation_timeouts_bounded(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})
