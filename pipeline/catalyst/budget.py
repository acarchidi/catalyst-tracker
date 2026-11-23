"""A run's time budget (ADR 0064): each step gets a deadline inside it, so a run ends cleanly.

GitHub Actions cancels a step at its limit, mid-write and without a summary. Instead each long
step stops starting new work once its share of the budget is spent; every step is safe to stop
and re-run, so the next run picks up the rest.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TimeBudget:
    """Minutes a run may take, counted from when the budget was made, on `clock` (seconds)."""

    minutes: float
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)

    def __post_init__(self) -> None:
        """Start counting now, and refuse a budget of no time."""
        if self.minutes <= 0:
            raise ValueError("a time budget must be more than zero minutes")
        self.started = self.clock()

    def by(self, share: float = 1.0) -> float:
        """Return the `clock` reading at which `share` of the budget is spent."""
        return self.started + self.minutes * 60 * share

    def past(self, share: float = 1.0) -> bool:
        """Say whether `share` of the budget is already spent."""
        return self.clock() > self.by(share)

    def minutes_used(self) -> float:
        """Return the minutes spent so far."""
        return (self.clock() - self.started) / 60
