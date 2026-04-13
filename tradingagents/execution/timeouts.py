from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def time_limit(seconds: float | int | None, *, timeout_message: str) -> Iterator[None]:
    """Raise TimeoutError if a blocking section exceeds the given wall-clock limit.

    This only arms a real timer on the main thread where SIGALRM is available.
    Other contexts degrade to a no-op so the code remains portable.
    """
    if not seconds or seconds <= 0:
        yield
        return

    if (
        threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
    ):
        yield
        return

    def _handle_timeout(signum, frame):  # pragma: no cover - signal handler
        raise TimeoutError(timeout_message)

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
