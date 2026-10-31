"""`make setup`: create the dev and test databases, migrate them, set the app-role password.

Idempotent. Uses DATABASE_ADMIN_URL for credentials/host and replaces the database name.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import dsn_database, dsn_with_database, get_settings
from app.core.migrate import ensure_database, migrate


async def main() -> int:
    s = get_settings()
    names = {dsn_database(s.database_admin_url), s.sp_test_db}
    for name in sorted(names):
        created = await ensure_database(s.database_admin_url, name)
        applied = await migrate(dsn_with_database(s.database_admin_url, name), app_password=s.app_db_password)
        print(
            f"database {name}: {'created' if created else 'exists'}, migrations applied: {applied or 'none'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
