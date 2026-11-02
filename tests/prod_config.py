"""A complete, valid production `Settings` kwargs set (shared by the config and security tests)."""

from __future__ import annotations

from typing import Any


def prod_kwargs(**over: Any) -> dict[str, Any]:
    """A complete, valid production configuration; tests override one field at a time."""
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": "prod",
        "auth_mode": "supabase",
        "supabase_jwks_url": "https://ref.supabase.co/auth/v1/.well-known/jwks.json",
        "jwt_issuer": "https://ref.supabase.co/auth/v1",
        "cors_origins": ["https://app.example"],
        "app_db_password": "k3y-Rotated-9f2c1a77d0",
        "database_url": "postgresql://stockpilot_app:k3y-Rotated-9f2c1a77d0@db.example:5432/postgres",
        "database_admin_url": "postgresql://postgres:An0ther-Strong-pw@db.example:5432/postgres",
        "sec_user_agent": "StockPilot ops@example.com",
    }
    base.update(over)
    return base
