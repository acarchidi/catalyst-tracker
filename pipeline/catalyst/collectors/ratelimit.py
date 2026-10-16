"""Spacing between requests to one source, so no source sees bursts."""

import threading
import time
from collections.abc import Callable
from typing import Protocol

SEC_MAX_REQUESTS_PER_SECOND = 10


class Limiter(Protocol):
    """Anything that can make a request wait its turn."""

    def wait(self) -> None:
        """Block until the next request may start."""
        ...


class NoWait:
    """A limiter that never waits; only for tests that replay recorded responses."""

    def wait(self) -> None:
        """Return at once."""


class MinIntervalLimiter:
    """Wait so that consecutive requests start at least `1 / max_per_second` seconds apart.

    Idle time is not saved up, so there are never bursts. The rate must stay below the
    SEC EDGAR limit of 10 requests per second, the strictest limit among our sources: at
    exactly 10, eleven requests can start inside one closed one-second window. Safe to
    share between threads.
    """

    def __init__(
        self,
        max_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a limiter; `clock` and `sleep` are replaceable for tests."""
        if not 0 < max_per_second < SEC_MAX_REQUESTS_PER_SECOND:
            raise ValueError(f"max_per_second must be above 0 and below {SEC_MAX_REQUESTS_PER_SECOND}")
        self._interval = 1.0 / max_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed: float | None = None

    def wait(self) -> None:
        """Block until the next request may start."""
        with self._lock:
            now = self._clock()
            if self._next_allowed is not None and now < self._next_allowed:
                self._sleep(self._next_allowed - now)
                now = self._next_allowed
            self._next_allowed = now + self._interval
