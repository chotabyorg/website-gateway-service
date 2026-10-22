"""Explicit common-session cohort and authenticated public HTTPS termination."""
from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network

from core.routing import MAX_SESSIONS, RoutingPolicy, _mapping, strict_uuid


@dataclass(frozen=True)
class WebsitePublicPolicy:
    integrations: dict[str, str]
    trusted_proxies: tuple
    message_sessions: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def parse(cls, integrations: str = "{}", trusted_proxies: str = "",
              message_session_allowlist: str = "{}"):
        selected = RoutingPolicy.parse(integrations).guards
        message_sessions = {}
        for key, sessions in _mapping(message_session_allowlist).items():
            integration = strict_uuid(key)
            if (integration not in selected or not isinstance(sessions, list)
                    or len(sessions) > MAX_SESSIONS):
                raise ValueError("invalid_common_message_session_allowlist")
            validated = frozenset(strict_uuid(session) for session in sessions)
            if len(validated) != len(sessions):
                raise ValueError("duplicate_common_message_session")
            message_sessions[integration] = validated
        entries = trusted_proxies.split(",") if trusted_proxies else []
        if len(entries) > 16:
            raise ValueError("invalid_public_proxy_configuration")
        networks = tuple(ip_network(value.strip(), strict=True) for value in entries)
        if any(network.prefixlen == 0 for network in networks) or len(set(networks)) != len(networks):
            raise ValueError("invalid_public_proxy_configuration")
        return cls(selected, networks, message_sessions)

    def admits_message(self, integration_id: str, session_id: str) -> bool:
        """New-input fence only; private capability and grant checks still apply."""
        return session_id in self.message_sessions.get(integration_id, frozenset())

    def client_ip(self, request) -> str:
        """Trust forwarding only from an explicitly selected actual TCP peer."""
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if not isinstance(peer, tuple) or not peer:
            raise ValueError("public_https_required")
        address = ip_address(peer[0])
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        trusted = any(address.version == network.version and address in network
                      for network in self.trusted_proxies)
        if trusted:
            proto = request.headers.getall("X-Forwarded-Proto", [])
            forwarded = request.headers.getall("X-Real-IP", [])
            if proto != ["https"] or len(forwarded) != 1:
                raise ValueError("public_https_required")
            address = ip_address(forwarded[0])
        elif not request.secure:
            raise ValueError("public_https_required")
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return str(address)
