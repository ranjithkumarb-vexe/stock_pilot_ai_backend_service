"""Error model (CONTRACT.md): HTTP status + body `{code, message, trace_id, details?}`."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, trace_id_var

log = get_logger("app.errors")


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    RULE_VIOLATION = "rule_violation"
    STALE_QUOTE = "stale_quote"
    MARKET_CLOSED = "market_closed"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_SHARES = "insufficient_shares"
    UNSUPPORTED_SYMBOL = "unsupported_symbol"
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    INTERNAL = "internal"


STATUS_FOR_CODE: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.CONFLICT: 409,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.RULE_VIOLATION: 422,
    ErrorCode.STALE_QUOTE: 409,
    ErrorCode.MARKET_CLOSED: 409,
    ErrorCode.INSUFFICIENT_CASH: 422,
    ErrorCode.INSUFFICIENT_SHARES: 422,
    ErrorCode.UNSUPPORTED_SYMBOL: 422,
    ErrorCode.GUARDRAIL_BLOCKED: 422,
    ErrorCode.UPSTREAM_UNAVAILABLE: 503,
    ErrorCode.INTERNAL: 500,
}

_CODE_FOR_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.UPSTREAM_UNAVAILABLE,
}


class AppError(Exception):
    """Raise anywhere in a request; rendered as the contract error body."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code or STATUS_FOR_CODE[code]
        self.headers = headers or {}


def unauthenticated(message: str = "Authentication required") -> AppError:
    return AppError(ErrorCode.UNAUTHENTICATED, message, headers={"WWW-Authenticate": "Bearer"})


def forbidden(message: str = "Forbidden") -> AppError:
    return AppError(ErrorCode.FORBIDDEN, message)


def not_found(message: str = "Not found") -> AppError:
    return AppError(ErrorCode.NOT_FOUND, message)


def conflict(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(ErrorCode.CONFLICT, message, details=details)


def validation_error(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(ErrorCode.VALIDATION_ERROR, message, details=details)


def upstream_unavailable(message: str = "Service temporarily unavailable") -> AppError:
    return AppError(ErrorCode.UPSTREAM_UNAVAILABLE, message)


def error_body(
    code: ErrorCode | str,
    message: str,
    details: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": str(code),
        "message": message,
        "trace_id": trace_id or trace_id_var.get(),
    }
    if details:
        body["details"] = details
    return body


def error_response(
    status_code: int,
    code: ErrorCode | str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    hdrs = dict(headers or {})
    hdrs["X-Trace-Id"] = trace_id_var.get()
    return JSONResponse(error_body(code, message, details), status_code=status_code, headers=hdrs)


def internal_error_response() -> JSONResponse:
    return error_response(500, ErrorCode.INTERNAL, "Internal server error")


async def _app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _CODE_FOR_STATUS.get(exc.status_code)
    if code is None:
        code = ErrorCode.INTERNAL if exc.status_code >= 500 else ErrorCode.VALIDATION_ERROR
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else code.value
    if exc.status_code == 404 and message == "Not Found":
        message = "Resource not found"
    headers = dict(exc.headers or {})
    return error_response(exc.status_code, code, message, None, headers)


async def _validation_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # never echo the offending input back (may contain secrets)
    errors = [
        {
            "loc": [str(p) for p in e.get("loc", ())],
            "msg": str(e.get("msg", "")),
            "type": e.get("type", ""),
        }
        for e in exc.errors()
    ]
    return error_response(422, ErrorCode.VALIDATION_ERROR, "Request validation failed", {"errors": errors})


async def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled exception", exc_info=exc)
    return internal_error_response()


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
