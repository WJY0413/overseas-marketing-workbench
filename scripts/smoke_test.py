"""Read-only smoke test for the public package."""

from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.main import app


def main() -> None:
    settings = get_settings()
    assert settings.dry_run_email is True, "public defaults must stay in dry-run mode"
    paths = {route.path for route in app.routes}
    required = {"/", "/health", "/senders", "/templates", "/settings"}
    missing = sorted(required - paths)
    assert not missing, f"missing routes: {missing}"
    print(f"smoke_ok routes={len(paths)} dry_run={settings.dry_run_email}")


if __name__ == "__main__":
    main()
