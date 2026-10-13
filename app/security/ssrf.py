"""SSRF guard for every outbound URL fetch.

- scheme must be https (http only if allow_http, used for GitHub clone URLs never; live URLs are https only)
- every resolved IP must be public (blocks loopback, private, link-local, reserved, multicast, metadata)
- redirects are followed manually and EVERY hop is re-validated
- response body is size-capped and the whole fetch is time-capped

Known limitation: validation and connection each resolve DNS, so a DNS-rebinding race is not
closed (production would pin the validated IP in a custom transport / use an egress proxy).
"""
import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings

log = logging.getLogger("ssrf")


class SSRFBlocked(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: dict
    body: bytes
    truncated: bool


_warned = False


def _allow_private() -> bool:
    """Test-only escape hatch so integration tests can hit a local HTTP server.

    Honored ONLY when APP_ENV=test. In any other environment the variable is ignored and a warning is logged.
    """
    global _warned
    if os.environ.get("SSRF_ALLOW_PRIVATE", "").lower() != "true":
        return False
    env = (os.environ.get("APP_ENV") or get_settings().app_env).lower()
    if env == "test":
        return True
    if not _warned:
        _warned = True
        log.warning("SSRF_ALLOW_PRIVATE is set but APP_ENV=%r is not 'test'; ignoring it (SSRF guard stays on)", env)
    return False


def is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped  # ::ffff:127.0.0.1 must not slip through
    return not addr.is_global or addr.is_multicast


def default_resolver(host: str) -> list[str]:
    return list({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def validate_url(url: str, *, require_https: bool = True, resolver: Callable[[str], list[str]] | None = None) -> str:
    parts = urlparse(url)
    allowed = {"https"} if require_https else {"https", "http"}
    if _allow_private():
        allowed = {"https", "http"}
    if parts.scheme not in allowed:
        raise SSRFBlocked(f"scheme {parts.scheme!r} not allowed")
    host = parts.hostname
    if not host:
        raise SSRFBlocked("missing host")
    if parts.username or parts.password:
        raise SSRFBlocked("credentials in URL not allowed")
    try:
        ips = [host] if _is_ip_literal(host) else (resolver or default_resolver)(host)
    except socket.gaierror as e:
        raise SSRFBlocked(f"cannot resolve {host}: {e}")
    if not ips:
        raise SSRFBlocked(f"no addresses for {host}")
    if not _allow_private():
        for ip in ips:
            if is_blocked_ip(ip):
                raise SSRFBlocked(f"{host} resolves to non-public address {ip}")
    return url


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def safe_get(
    url: str,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    timeout: float = 10.0,
    max_redirects: int = 5,
    require_https: bool = True,
    resolver: Callable[[str], list[str]] | None = None,
    transport: httpx.BaseTransport | None = None,
    headers: dict | None = None,
) -> FetchResult:
    deadline = time.monotonic() + timeout
    current = url
    with httpx.Client(transport=transport, follow_redirects=False, timeout=timeout, headers=headers) as client:
        for _ in range(max_redirects + 1):
            validate_url(current, require_https=require_https, resolver=resolver)  # every hop
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise SSRFBlocked("redirect without Location")
                    current = urljoin(current, loc)
                    continue
                body = bytearray()
                truncated = False
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        del body[max_bytes:]
                        truncated = True
                        break
                    if time.monotonic() > deadline:
                        truncated = True
                        break
                return FetchResult(current, resp.status_code, dict(resp.headers), bytes(body), truncated)
    raise SSRFBlocked(f"too many redirects (> {max_redirects})")
