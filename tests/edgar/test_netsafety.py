"""T-SEC-04: SSRF guard for outbound fetches."""

from __future__ import annotations

import pytest

from app.modules.edgar.netsafety import UrlRejectedError, check_url_syntax, is_public_ip, validate_url


async def _public(_host: str) -> list[str]:
    return ["104.18.32.7"]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://10.0.0.5/x",
        "https://192.168.1.1/",
        "http://www.sec.gov/Archives/x",  # plain http
        "https://user:pw@www.sec.gov/x",  # credentials
        "https://www.sec.gov:8443/x",  # port
        "https://evil.example.com/x",
        "https://sec.gov.evil.example.com/x",
        "https://notsec.gov/x",
        "https://evilsec.gov/x",
        "ftp://www.sec.gov/x",
        "file:///etc/passwd",
        "https:///nohost",
        "https://2852039166/",  # decimal-encoded 169.254.169.254
    ],
)
async def test_dangerous_urls_rejected(url: str) -> None:
    with pytest.raises(UrlRejectedError):
        await validate_url(url, resolver=_public)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov/Archives/edgar/data/320193/x.htm",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        "https://sec.gov/robots.txt",
        "https://WWW.SEC.GOV./x",
    ],
)
async def test_allowlisted_urls_accepted(url: str) -> None:
    await validate_url(url, resolver=_public)


@pytest.mark.parametrize(
    "addr",
    ["169.254.169.254", "10.1.2.3", "127.0.0.1", "::1", "fe80::1", "0.0.0.0", "::ffff:169.254.169.254"],  # noqa: S104
)
async def test_allowlisted_host_resolving_to_private_address_is_rejected(addr: str) -> None:
    async def resolver(_h: str) -> list[str]:
        return ["104.18.32.7", addr]  # one good, one bad -> reject

    with pytest.raises(UrlRejectedError):
        await validate_url("https://www.sec.gov/x", resolver=resolver)


async def test_unresolvable_host_rejected() -> None:
    async def resolver(_h: str) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(UrlRejectedError):
        await validate_url("https://www.sec.gov/x", resolver=resolver)

    async def empty(_h: str) -> list[str]:
        return []

    with pytest.raises(UrlRejectedError):
        await validate_url("https://www.sec.gov/x", resolver=empty)


def test_is_public_ip() -> None:
    assert is_public_ip("104.18.32.7")
    assert is_public_ip("2606:4700:4700::1111")
    assert not is_public_ip("not-an-ip")
    assert not is_public_ip("100.64.0.1")  # CGNAT, not global
    assert not is_public_ip("169.254.169.254")


def test_syntax_returns_host() -> None:
    assert check_url_syntax("https://Data.SEC.gov/x") == "data.sec.gov"
