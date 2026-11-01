"""SEC EDGAR HTTP client (SPEC 6.2, 7.5; tests T-DATA-01, T-SEC-04).

* Declared `User-Agent` with a contact email (SEC fair-access policy); the placeholder default is refused.
* At most `rate_per_s` (default 8, SEC allows 10) requests in any sliding one-second window, shared by all
  concurrent callers of one client instance.
* 429 / 403 / 5xx / network errors -> exponential backoff with jitter, honouring `Retry-After`.
* Host allow-list + private-IP block + no blind redirects (see `netsafety`), 25 MB body cap, timeouts.
* On-disk response cache (default `~/.cache/stockpilot/edgar`, override with `EDGAR_CACHE_DIR`) so
  re-runs and resumed ingests do not hit the SEC again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.modules.edgar.netsafety import (
    ALLOWED_HOSTS,
    Resolver,
    UrlRejectedError,
    system_resolver,
    validate_url,
)

log = get_logger("app.edgar.client")

DATA_HOST = "https://data.sec.gov"
WWW_HOST = "https://www.sec.gov"
MAX_BYTES = 25 * 1024 * 1024
RETRY_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_EMAIL_RE = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}")


class EdgarError(Exception):
    """Base class for EDGAR client failures."""


class EdgarNotFoundError(EdgarError):
    """HTTP 404 from EDGAR (not retried)."""


class EdgarUnavailableError(EdgarError):
    """Retries exhausted (429/403/5xx/network)."""


def validate_user_agent(user_agent: str) -> str:
    ua = user_agent.strip().strip('"').strip("'")
    if not _EMAIL_RE.search(ua) or "your-email" in ua.lower():
        raise ValueError(
            "SEC_USER_AGENT must declare a real contact email, e.g. 'StockPilot you@example.com'"
        )
    return ua


def companyfacts_url(cik: str | int) -> str:
    return f"{DATA_HOST}/api/xbrl/companyfacts/CIK{int(cik):010d}.json"


def submissions_url(cik: str | int) -> str:
    return f"{DATA_HOST}/submissions/CIK{int(cik):010d}.json"


def archive_url(cik: str | int, accession_no: str, document: str) -> str:
    return f"{WWW_HOST}/Archives/edgar/data/{int(cik)}/{accession_no.replace('-', '')}/{document}"


def default_cache_dir() -> Path:
    env = os.environ.get("EDGAR_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "stockpilot" / "edgar"


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int
    body: bytes
    fetched_at: datetime
    from_cache: bool

    def json(self) -> Any:
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class SlidingWindowLimiter:
    """No more than `limit` acquisitions in any `window_s` seconds (monotonic clock, async-safe)."""

    def __init__(self, limit: int, clock: Clock, window_s: float = 1.0) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._window = window_s
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock.monotonic()
                while self._stamps and now - self._stamps[0] >= self._window:
                    self._stamps.popleft()
                if len(self._stamps) < self._limit:
                    self._stamps.append(now)
                    return
                await self._clock.sleep(self._window - (now - self._stamps[0]) + 1e-3)


class DiskCache:
    """URL -> bytes cache with a small JSON sidecar (`fetched_at`)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode()).hexdigest()
        base = self.root / digest[:2] / digest
        return base.with_suffix(".bin"), base.with_suffix(".json")

    def path_for(self, url: str) -> Path:
        return self._paths(url)[0]

    def get(self, url: str, now: datetime, max_age_s: float | None) -> FetchResult | None:
        body_p, meta_p = self._paths(url)
        try:
            meta = json.loads(meta_p.read_text())
            body = body_p.read_bytes()
        except (OSError, ValueError):
            return None
        fetched_at = datetime.fromisoformat(meta["fetched_at"])
        if max_age_s is not None and (now - fetched_at).total_seconds() > max_age_s:
            return None
        return FetchResult(url=url, status=200, body=body, fetched_at=fetched_at, from_cache=True)

    def put(self, url: str, body: bytes, fetched_at: datetime) -> None:
        body_p, meta_p = self._paths(url)
        body_p.parent.mkdir(parents=True, exist_ok=True)
        tmp = body_p.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(body_p)
        meta_p.write_text(json.dumps({"url": url, "fetched_at": fetched_at.isoformat()}))


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        *,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_dir: Path | None = None,
        rate_per_s: int = 8,
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 60.0,
        timeout_s: float = 30.0,
        max_bytes: int = MAX_BYTES,
        resolver: Resolver = system_resolver,
        rng: random.Random | None = None,
        allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    ) -> None:
        self.user_agent = validate_user_agent(user_agent)
        self.clock = clock or SystemClock()
        self._limiter = SlidingWindowLimiter(rate_per_s, self.clock)
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._backoff_cap = backoff_cap_s
        self._max_bytes = max_bytes
        self._resolver = resolver
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._allowed_hosts = allowed_hosts
        self._cache = DiskCache(cache_dir if cache_dir is not None else default_cache_dir())
        self.request_count = 0
        self._http = httpx.AsyncClient(
            transport=transport,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    def cache_path(self, url: str) -> str:
        """Where the body of `url` lives on disk once fetched (recorded as `raw_documents.storage_path`)."""
        return str(self._cache.path_for(url))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> EdgarClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ public API
    async def get(self, url: str, *, max_age_s: float | None = None, force: bool = False) -> FetchResult:
        """GET with cache. `max_age_s=None` means "any cached copy is fine" (immutable documents)."""
        await validate_url(url, resolver=self._resolver, allowed_hosts=self._allowed_hosts)
        if not force:
            hit = self._cache.get(url, self.clock.now(), max_age_s)
            if hit is not None:
                return hit
        body = await self._fetch_with_retries(url)
        fetched_at = self.clock.now()
        self._cache.put(url, body, fetched_at)
        return FetchResult(url=url, status=200, body=body, fetched_at=fetched_at, from_cache=False)

    async def get_json(self, url: str, *, max_age_s: float | None = None, force: bool = False) -> Any:
        return (await self.get(url, max_age_s=max_age_s, force=force)).json()

    # ------------------------------------------------------------------ internals
    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        expo = float(min(self._backoff_cap, self._backoff_base * (2**attempt)))
        delay = expo * (0.5 + self._rng.random() * 0.5)  # equal jitter
        hinted = self._parse_retry_after(retry_after)
        if hinted is not None:
            delay = max(delay, min(hinted, self._backoff_cap))
        return delay

    def _parse_retry_after(self, value: str | None) -> float | None:
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return float(value)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - self.clock.now()).total_seconds())

    async def _fetch_with_retries(self, url: str) -> bytes:
        attempt = 0
        hops = 0
        current = url
        last_error = "unknown"
        while True:
            await self._limiter.acquire()
            self.request_count += 1
            retry_after: str | None = None
            try:
                async with self._http.stream("GET", current) as resp:
                    status = resp.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        hops += 1
                        if not location or hops > 3:
                            raise EdgarError("too many or empty redirects")
                        current = urljoin(current, location)
                        await validate_url(
                            current, resolver=self._resolver, allowed_hosts=self._allowed_hosts
                        )
                        continue
                    if status == 404:
                        raise EdgarNotFoundError(url)
                    if status in RETRY_STATUSES:
                        retry_after = resp.headers.get("retry-after")
                        last_error = f"HTTP {status}"
                    elif status != 200:
                        raise EdgarError(f"unexpected HTTP {status} for {url}")
                    else:
                        return await self._read_body(resp, url)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = type(exc).__name__
            if attempt >= self._max_retries:
                raise EdgarUnavailableError(f"giving up on {url} after {attempt + 1} attempts ({last_error})")
            delay = self._retry_delay(attempt, retry_after)
            log.warning(
                "edgar retry",
                extra={"reason": last_error, "attempt": attempt + 1, "delay_s": round(delay, 2)},
            )
            attempt += 1
            await self.clock.sleep(delay)

    async def _read_body(self, resp: httpx.Response, url: str) -> bytes:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            raise EdgarError(f"response for {url} exceeds the {self._max_bytes} byte cap")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                raise EdgarError(f"response for {url} exceeds the {self._max_bytes} byte cap")
            chunks.append(chunk)
        return b"".join(chunks)


__all__ = [
    "DATA_HOST",
    "WWW_HOST",
    "DiskCache",
    "EdgarClient",
    "EdgarError",
    "EdgarNotFoundError",
    "EdgarUnavailableError",
    "FetchResult",
    "SlidingWindowLimiter",
    "UrlRejectedError",
    "archive_url",
    "companyfacts_url",
    "default_cache_dir",
    "submissions_url",
    "validate_user_agent",
]
