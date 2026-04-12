from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = REPO_ROOT / "runtime" / "public_backend_url.txt"
TARGET_PATH = REPO_ROOT / "dashboard_proxy_url.txt"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def main() -> int:
    if not SOURCE_PATH.exists():
        return 0

    new_url = SOURCE_PATH.read_text(encoding="utf-8").strip()
    if not new_url:
        return 0

    current_url = TARGET_PATH.read_text(encoding="utf-8").strip() if TARGET_PATH.exists() else ""
    if current_url == new_url:
        return 0

    TARGET_PATH.write_text(new_url + "\n", encoding="utf-8")

    add_result = _run("git", "add", "dashboard_proxy_url.txt")
    if add_result.returncode != 0:
        sys.stderr.write(add_result.stderr)
        return add_result.returncode

    diff_result = _run("git", "diff", "--cached", "--quiet", "--", "dashboard_proxy_url.txt")
    if diff_result.returncode == 0:
        return 0

    commit_result = _run("git", "commit", "-m", f"Update dashboard proxy URL to {new_url}")
    if commit_result.returncode != 0:
        if "nothing to commit" in commit_result.stdout.lower() or "nothing to commit" in commit_result.stderr.lower():
            return 0
        sys.stderr.write(commit_result.stdout)
        sys.stderr.write(commit_result.stderr)
        return commit_result.returncode

    push_result = _run("git", "push", "origin", "main")
    if push_result.returncode != 0:
        sys.stderr.write(push_result.stdout)
        sys.stderr.write(push_result.stderr)
        return push_result.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
