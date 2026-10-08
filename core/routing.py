"""Bounded routing policy. Trusted integration identity is the only cohort key."""
from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

MAX_INTEGRATIONS = 64
MAX_SESSIONS = 64


def strict_uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid_uuid")
    try:
        parsed = str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("invalid_uuid") from None
    if parsed != value:
        raise ValueError("invalid_uuid")
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_routing_key")
        result[key] = value
    return result


def _mapping(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw) > 200_000:
        raise ValueError("invalid_routing_configuration")
    try:
        result = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        raise ValueError("invalid_routing_configuration") from None
    if not isinstance(result, dict) or len(result) > MAX_INTEGRATIONS:
        raise ValueError("invalid_routing_configuration")
    return result


@dataclass(frozen=True)
class Cohort:
    expected_agent_id: str
    mode: str
    session_ids: frozenset[str]

    def selects(self, session_id: object) -> bool:
        # INTEGRATION_ALL is a source capability, never an inferred prod switch.
        return self.mode == "INTEGRATION_ALL" or session_id in self.session_ids


@dataclass(frozen=True)
class RoutingPolicy:
    guards: dict[str, str]
    cohorts: dict[str, Cohort]

    @classmethod
    def parse(cls, guards_json: str = "{}", cohorts_json: str = "{}") -> RoutingPolicy:
        guards = {strict_uuid(key): strict_uuid(value) for key, value in _mapping(guards_json).items()}
        cohorts = {}
        for key, item in _mapping(cohorts_json).items():
            integration = strict_uuid(key)
            if not isinstance(item, dict) or set(item) != {"expected_agent_id", "mode", "session_ids"}:
                raise ValueError("invalid_cohort_configuration")
            agent = strict_uuid(item["expected_agent_id"])
            mode = item["mode"]
            sessions = item["session_ids"]
            if mode not in ("SESSION_ALLOWLIST", "INTEGRATION_ALL") or not isinstance(sessions, list):
                raise ValueError("invalid_cohort_configuration")
            if len(sessions) > MAX_SESSIONS:
                raise ValueError("invalid_cohort_configuration")
            validated = frozenset(strict_uuid(value) for value in sessions)
            if len(validated) != len(sessions):
                raise ValueError("duplicate_session_id")
            if (mode == "SESSION_ALLOWLIST" and not validated) or (mode == "INTEGRATION_ALL" and validated):
                raise ValueError("invalid_cohort_configuration")
            if guards.get(integration) != agent:
                raise ValueError("cohort_requires_matching_guard")
            cohorts[integration] = Cohort(agent, mode, validated)
        return cls(guards, cohorts)

    def selected(self, integration_id: str, session_id: object) -> bool:
        cohort = self.cohorts.get(integration_id)
        # Public legacy session values can be opaque, including non-hashable JSON.
        if cohort is None:
            return False
        if cohort.mode == "INTEGRATION_ALL":
            return True
        return isinstance(session_id, str) and cohort.selects(session_id)
