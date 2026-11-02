"""Auth/session endpoints: dev-login, /me, stream token, account deletion."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.core.auth import create_stream_token, issue_dev_token
from app.core.clock import Clock
from app.core.db import user_tx
from app.core.deps import CurrentUser, SettingsDep, UserConn
from app.core.errors import not_found, upstream_unavailable
from app.core.logging import get_logger
from app.core.users import KnownUsers
from app.modules.auth import service
from app.modules.auth.schemas import (
    DeleteAccountRequest,
    DevLoginRequest,
    DevLoginResponse,
    MeResponse,
    StreamTokenResponse,
)
from app.modules.profile import supabase_admin

router = APIRouter(tags=["auth"])
log = get_logger(__name__)


@router.post("/auth/dev-login", response_model=DevLoginResponse, summary="Dev-only persona login")
async def dev_login(body: DevLoginRequest, request: Request, settings: SettingsDep) -> DevLoginResponse:
    """`AUTH_MODE=dev` only (404 otherwise). Returns a dev-signed JWT with the same claims as Supabase."""
    if settings.auth_mode != "dev":
        raise not_found()
    me = await service.dev_login_user(body.persona, body.email)
    clock: Clock = request.app.state.clock
    token = issue_dev_token(settings, me.user_id, me.email, now=clock.now())
    return DevLoginResponse(access_token=token, expires_in=settings.dev_token_ttl_s, user=me)


@router.get("/me", response_model=MeResponse, summary="Current user")
async def me(user: CurrentUser, conn: UserConn) -> MeResponse:
    return await service.fetch_me(conn, user.user_id)


@router.post("/stream/token", response_model=StreamTokenResponse, summary="Single-use SSE token (<=60 s)")
async def stream_token(user: CurrentUser, request: Request, settings: SettingsDep) -> StreamTokenResponse:
    clock: Clock = request.app.state.clock
    async with user_tx(user.user_id) as conn:
        token = await create_stream_token(conn, user.user_id, clock.now(), settings.stream_token_ttl_s)
    return StreamTokenResponse(token=token, expires_in=settings.stream_token_ttl_s)


@router.delete("/account", status_code=204, summary="Delete the account and all of its data")
async def delete_account(
    body: DeleteAccountRequest,
    user: CurrentUser,
    conn: UserConn,
    request: Request,
    settings: SettingsDep,
) -> Response:
    del body  # validated: {"confirm": "DELETE"}
    clock: Clock = request.app.state.clock
    now = clock.now()
    await service.delete_account(conn, user.user_id, now)
    # CR-3: also remove the Supabase auth user (no-op without SUPABASE_URL + service-role key). It runs
    # before the local transaction commits: if Supabase is down we roll the local delete back (503) and the
    # client can simply retry; deleting an already-deleted auth user is a success (404 is treated as done).
    try:
        await supabase_admin.delete_auth_user(settings, user.user_id)
    except Exception as exc:
        log.error("supabase auth user deletion failed", extra={"error_type": type(exc).__name__})
        raise upstream_unavailable(
            "Could not finish deleting your sign-in account. Please try again."
        ) from exc
    known: KnownUsers = request.app.state.known_users
    known.mark_deleted(user.user_id, now)
    return Response(status_code=204)
