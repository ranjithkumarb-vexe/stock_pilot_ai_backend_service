"""Pure-ASGI middlewares (no BaseHTTPMiddleware: it breaks SSE streaming and context vars).

Stack, outermost first: SecurityHeaders -> Trace(+access log) -> CORS -> ErrorBoundary -> BodyLimit ->
IpRateLimit -> router. Every response (CORS preflights included) carries `X-Trace-Id` because Trace wraps
`send` for everything inside it; a 500 (ErrorBoundary), 413 or 429 sits inside CORS so it still gets CORS
headers and the browser can read the contract error body.
"""

from __future__ import annotations

import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ErrorCode, error_response, internal_error_response
from app.core.logging import get_logger, trace_id_var, user_id_var

log = get_logger("app.access")
err_log = get_logger("app.errors")

_TRACE_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_doc = scope["path"].startswith(_DOC_PATHS)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS.items():
                    headers.setdefault(k, v)
                if not is_doc:
                    headers.setdefault("Content-Security-Policy", _API_CSP)
                headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class TraceMiddleware:
    """Assigns/propagates the trace id, sets `X-Trace-Id`, writes one access-log line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for name, value in scope["headers"]:
            if name == b"x-trace-id":
                incoming = value.decode("latin-1")
                break
        trace_id = incoming if _TRACE_RE.match(incoming) else uuid.uuid4().hex
        trace_token = trace_id_var.set(trace_id)
        user_token = user_id_var.set(None)
        status = 0
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Trace-Id"] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],  # never the query string (stream tokens live there)
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            trace_id_var.reset(trace_token)
            user_id_var.reset(user_token)


class ErrorBoundaryMiddleware:
    """Converts any unhandled exception into the contract 500 body (so outer middlewares still run)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            err_log.exception("unhandled exception")
            if started:
                raise
            await internal_error_response()(scope, receive, send)


class BodyLimitMiddleware:
    """Rejects bodies above `max_bytes`: Content-Length fast path, plus a counter for chunked uploads
    (on overflow the 413 is sent immediately and the app sees a client disconnect)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await self._reject()(scope, receive, send)
                    return
                break
        received = 0
        rejected = False
        started = False

        async def receive_wrapper() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes and not started:
                    rejected = True
                    await self._reject()(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if rejected:
                return  # our 413 is already on the wire; drop whatever the app tries to add
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)

    def _reject(self) -> JSONResponse:
        return error_response(
            413,
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"Request body exceeds {self.max_bytes} bytes",
            {"max_bytes": self.max_bytes},
        )
