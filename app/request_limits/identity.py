"""Use the server's resolved socket address, never untrusted forwarding headers."""

import hashlib
import hmac
from ipaddress import ip_address, ip_network

from starlette.types import Scope


def client_address(scope: Scope) -> str:
    client = scope.get("client")
    if not client:
        return "unknown"
    try:
        address = ip_address(client[0])
    except ValueError:
        return "unknown"
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        # One budget per /64 prevents cheap address rotation within a subnet.
        return str(ip_network(f"{address}/64", strict=False))
    return str(address)


def opaque_key(secret: str, kind: str, value: str) -> str:
    digest = hmac.new(secret.encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
    return f"jobping:ratelimit:v1:{kind}:{digest}"
