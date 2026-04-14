from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = REPO_ROOT / "runtime" / "public_backend_url.txt"
TARGET_PATH = REPO_ROOT / "dashboard_proxy_url.txt"
ENABLE_REPO_SYNC_ENV = "TRADINGAGENTS_SYNC_DASHBOARD_PROXY_TO_REPO"


def _repo_sync_enabled() -> bool:
    value = os.getenv(ENABLE_REPO_SYNC_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def main() -> int:
    if not SOURCE_PATH.exists():
        return 0

    new_url = SOURCE_PATH.read_text(encoding="utf-8").strip()
    if not new_url:
        return 0

    # Automatic commit/push loops made the repo noisy. Local runtime consumers now
    # read the tunnel files directly, so writing the repo fallback is opt-in only.
    if not _repo_sync_enabled():
        return 0

    current_url = TARGET_PATH.read_text(encoding="utf-8").strip() if TARGET_PATH.exists() else ""
    if current_url == new_url:
        return 0

    TARGET_PATH.write_text(new_url + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
