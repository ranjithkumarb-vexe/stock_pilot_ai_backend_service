"""Write the API contract source of truth: docs/contracts/openapi.json (no database needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.core.config import Settings
from app.main import create_app

OUT = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "openapi.json"


def main() -> int:
    # fixed settings so the document does not depend on the developer's .env
    settings = Settings(_env_file=None, app_env="dev", auth_mode="dev", cors_origins=[])
    schema = create_app(settings).openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
