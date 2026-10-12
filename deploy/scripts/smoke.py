"""Read-only bounded deployment checks, including the actual version behind Nginx."""

import argparse
import json
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("sha")
    parser.add_argument("color")
    parser.add_argument("--proxy", action="store_true")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    if args.proxy:
        # Preserve TLS hostname/certificate verification while resolving to loopback.
        import socket

        original = socket.getaddrinfo

        def resolve(host: str, port: int, *rest: object) -> list:
            return original("127.0.0.1", port, *rest)

        socket.getaddrinfo = resolve
    paths = ["/api/v1/stats", "/api/v1/jobs?limit=1"]
    if args.color != "legacy":
        paths = ["/health", "/ready", "/version", *paths]
    for path in paths:
        request = urllib.request.Request(base + path, headers={"Connection": "close"})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.load(response)
        if path == "/version" and data != {"commit": args.sha, "color": args.color}:
            raise SystemExit("Proxy/target version mismatch")
    print(f"Smoke passed color={args.color} commit={args.sha}")


if __name__ == "__main__":
    main()
