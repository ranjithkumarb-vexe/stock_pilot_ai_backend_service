"""Outbound-fetch safety (SPEC 11.2 "SSRF/fetching", T-SEC-04).

* Hosts are matched against an exact allow-list (`sec.gov`, `www.sec.gov`, `data.sec.gov`).
* Only `https`, no credentials in the URL, no non-default port.
* The host is resolved and *every* returned address must be a public unicast address (private, loopback,
  link-local incl. 169.254.169.254, multicast, reserved and unspecified ranges are rejected). IP literals
  are never accepted as hosts.
* Redirects are not followed blindly: the caller re-validates each hop with `validate_url`.

Residual risk (documented): the check resolves the name itself and the HTTP stack resolves it again when
connecting, so a hostile resolver could in theory rebind between the two. The allow-list contains only
sec.gov names and redirects are re-validated, which keeps the exposure low for the MVP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

ALLOWED_HOSTS: frozenset[str] = frozenset({"sec.gov", "www.sec.gov", "data.sec.gov"})

Resolver = Callable[[str], Awaitable[list[str]]]


class UrlRejectedError(ValueError):
    """The URL is not allowed to be fetched (SSRF guard)."""


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(value: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def check_url_syntax(url: str, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> str:
    """Synchronous part of the guard; returns the (lower-cased) host."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UrlRejectedError("only https URLs may be fetched")
    if parts.username or parts.password:
        raise UrlRejectedError("credentials in URL are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlRejectedError("URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UrlRejectedError("IP literals are not allowed")
    if host not in allowed_hosts:
        raise UrlRejectedError(f"host {host!r} is not on the fetch allow-list")
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlRejectedError("invalid port") from exc
    if port not in (None, 443):
        raise UrlRejectedError("non-default ports are not allowed")
    return host


async def validate_url(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
) -> None:
    """Raise `UrlRejectedError` unless `url` may be fetched."""
    host = check_url_syntax(url, allowed_hosts)
    try:
        addresses = await resolver(host)
    except OSError as exc:
        raise UrlRejectedError(f"cannot resolve {host!r}") from exc
    if not addresses:
        raise UrlRejectedError(f"{host!r} did not resolve")
    for addr in addresses:
        if not is_public_ip(addr):
            raise UrlRejectedError(f"{host!r} resolves to a non-public address")
