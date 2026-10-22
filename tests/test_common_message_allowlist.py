"""Startup parsing rejects broad, ambiguous and unbounded common admission."""
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.website_public_policy import WebsitePublicPolicy

INTEGRATION, AGENT, SESSION = (str(uuid4()) for _ in range(3))
COMMON = json.dumps({INTEGRATION: AGENT})


@pytest.mark.parametrize("raw", [
    "", "null", "[]", "true", "{", " " * 200001,
    json.dumps({INTEGRATION: "INTEGRATION_ALL"}),
    json.dumps({INTEGRATION: {"mode": "INTEGRATION_ALL", "session_ids": []}}),
    json.dumps({INTEGRATION: None}),
    json.dumps({INTEGRATION: [SESSION, SESSION]}),
    json.dumps({INTEGRATION: ["*"]}),
    json.dumps({INTEGRATION: [SESSION.upper()]}),
    json.dumps({INTEGRATION: [SESSION.replace("-", "")]}),
    json.dumps({INTEGRATION: [None]}),
    json.dumps({INTEGRATION: [True]}),
    json.dumps({INTEGRATION: [[SESSION]]}),
    json.dumps({INTEGRATION: [str(uuid4()) for _ in range(65)]}),
    json.dumps({"*": [SESSION]}),
    json.dumps({INTEGRATION.upper(): [SESSION]}),
    json.dumps({str(uuid4()): [SESSION]}),
    json.dumps({str(uuid4()): [] for _ in range(65)}),
    '{"' + INTEGRATION + '":[],"' + INTEGRATION + '":["' + SESSION + '"]}',
], ids=["blank", "null", "array", "bool", "malformed", "oversize", "broad-string",
        "broad-object", "null-sessions", "duplicate-session", "wildcard-session",
        "uppercase-session", "compact-session", "null-session", "bool-session",
        "nested-session", "too-many-sessions", "wildcard-integration",
        "uppercase-integration", "unselected-integration", "too-many-integrations",
        "duplicate-integration"])
def test_malformed_or_broad_allowlist_fails_closed(raw):
    with pytest.raises(ValueError):
        WebsitePublicPolicy.parse(COMMON, message_session_allowlist=raw)


def test_default_and_explicit_empty_lists_select_no_messages():
    for raw in ("{}", json.dumps({INTEGRATION: []})):
        policy = WebsitePublicPolicy.parse(COMMON, message_session_allowlist=raw)
        assert not policy.admits_message(INTEGRATION, SESSION)


def test_exact_session_pairs_with_bounded_lists():
    sessions = [str(uuid4()) for _ in range(64)]
    policy = WebsitePublicPolicy.parse(COMMON,
        message_session_allowlist=json.dumps({INTEGRATION: sessions}))
    assert all(policy.admits_message(INTEGRATION, session) for session in sessions)
    assert not policy.admits_message(str(uuid4()), sessions[0])
    assert not policy.admits_message(INTEGRATION, str(uuid4()))


def test_settings_load_explicit_env_and_reject_unselected_integration(load_module, monkeypatch):
    config = load_module("core.config")
    assert config.Settings(_env_file=None).COMMON_WEBSITE_MESSAGE_SESSION_ALLOWLIST == "{}"
    selected = json.dumps({INTEGRATION: [SESSION]})
    monkeypatch.setenv("COMMON_WEBSITE_MESSAGE_SESSION_ALLOWLIST", selected)
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None)
    settings = config.Settings(_env_file=None, COMMON_WEBSITE_INTEGRATIONS=COMMON,
        MANAGED_WEBSITE_GUARDED_INTEGRATIONS=COMMON,
        CONVERSATION_SERVICE_URL="http://old-conversation", CONVERSATION_S2S_TOKEN="fake-v2",
        COMMON_WEBSITE_CONVERSATION_URL="https://conversation.internal.example",
        COMMON_WEBSITE_CONVERSATION_PEER="conversation.internal.example",
        COMMON_WEBSITE_S2S_TOKEN="fake-private")
    assert settings.public_session_policy().admits_message(INTEGRATION, SESSION)
