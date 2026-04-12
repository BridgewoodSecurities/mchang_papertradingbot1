from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_BACKEND_FILE = RUNTIME_DIR / "public_backend_url.txt"
PUBLIC_BACKEND_API_FILE = RUNTIME_DIR / "public_backend_api_url.txt"
TUNNEL_LOG = RUNTIME_DIR / "launchd" / "tunnel-discovered-url.log"
TUNNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
TUNNEL_URL_PATTERN = re.compile(
    r"tunneled with tls termination,\s*(https://[A-Za-z0-9.-]+)",
    re.IGNORECASE,
)


def main() -> int:
    command = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-R",
        "80:127.0.0.1:8000",
        "nokey@localhost.run",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        match = TUNNEL_URL_PATTERN.search(line)
        if match:
            base_url = match.group(1).rstrip("/")
            PUBLIC_BACKEND_FILE.write_text(base_url + "\n", encoding="utf-8")
            PUBLIC_BACKEND_API_FILE.write_text(f"{base_url}/api/overview\n", encoding="utf-8")
            TUNNEL_LOG.write_text(base_url + "\n", encoding="utf-8")

    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
