"""Deleting the Supabase auth user after local data deletion (CR-3).

Needs `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`. Unverified against a real project (no credentials here);
tested with a mocked HTTP transport. The foundation's `DELETE /account` can call `delete_auth_user` once wired.
"""

from __future__ import annotations

from uuid import UUID

import httpx

from app.core.config import Settings


def configured(settings: Settings) -> bool:
    return bool(settings.supabase_url and settings.supabase_service_role_key)


async def delete_auth_user(
    settings: Settings, user_id: UUID, client: httpx.AsyncClient | None = None
) -> bool:
    """True when the auth user is gone (204 or already 404). False when not configured. Raises on other errors."""
    if not configured(settings):
        return False
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}"
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    own = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await http.delete(url, headers=headers)
    finally:
        if own:
            await http.aclose()
    if resp.status_code in (200, 204, 404):
        return True
    resp.raise_for_status()
    return False
