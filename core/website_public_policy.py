"""Explicit common-session cohort and authenticated public HTTPS termination."""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_network

from core.routing import RoutingPolicy


@dataclass(frozen=True)
class WebsitePublicPolicy:
    integrations: dict[str, str]
    trusted_proxies: tuple

    @classmethod
    def parse(cls, integrations: str = "{}", trusted_proxies: str = ""):
        selected = RoutingPolicy.parse(integrations).guards
        entries = trusted_proxies.split(",") if trusted_proxies else []
        if len(entries) > 16:
            raise ValueError("invalid_public_proxy_configuration")
        networks = tuple(ip_network(value.strip(), strict=True) for value in entries)
        if any(network.prefixlen == 0 for network in networks) or len(set(networks)) != len(networks):
            raise ValueError("invalid_public_proxy_configuration")
        return cls(selected, networks)

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
