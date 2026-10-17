"""The rate limiter keeps SEC EDGAR requests under its 10-per-second limit (spec: Data sources)."""

import itertools
import threading

import pytest

from catalyst.collectors.ratelimit import MinIntervalLimiter


class FakeClock:
    """A clock that only moves when the limiter sleeps."""

    def __init__(self) -> None:
        self.now = 100.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


def test_no_two_requests_closer_than_the_interval() -> None:
    clock = FakeClock()
    limiter = MinIntervalLimiter(max_per_second=8, clock=clock.time, sleep=clock.sleep)
    starts: list[float] = []
    for _ in range(25):
        limiter.wait()
        starts.append(clock.now)
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert min(gaps) >= 1 / 8 - 1e-9


def test_no_one_second_window_holds_more_than_ten_requests() -> None:
    clock = FakeClock()
    limiter = MinIntervalLimiter(max_per_second=8, clock=clock.time, sleep=clock.sleep)
    starts: list[float] = []
    for _ in range(50):
        limiter.wait()
        starts.append(clock.now)
    for first in starts:
        assert sum(1 for t in starts if first <= t <= first + 1.0) <= 10


def test_first_request_does_not_wait() -> None:
    clock = FakeClock()
    limiter = MinIntervalLimiter(max_per_second=8, clock=clock.time, sleep=clock.sleep)
    limiter.wait()
    assert clock.now == 100.0


def test_idle_time_is_not_banked_into_a_burst() -> None:
    clock = FakeClock()
    limiter = MinIntervalLimiter(max_per_second=8, clock=clock.time, sleep=clock.sleep)
    limiter.wait()
    clock.now += 60
    limiter.wait()
    before = clock.now
    limiter.wait()
    assert clock.now - before == pytest.approx(1 / 8)


@pytest.mark.parametrize("rate", [0, -1, 10, 10.5, 11])
def test_rates_above_the_sec_limit_or_non_positive_are_refused(rate: float) -> None:
    with pytest.raises(ValueError, match="max_per_second"):
        MinIntervalLimiter(max_per_second=rate)


def test_threads_sharing_one_limiter_never_start_together() -> None:
    limiter = MinIntervalLimiter(max_per_second=8)
    starts: list[float] = []

    def worker() -> None:
        # The limiter's own start times: a thread's later clock reading can lag behind its turn
        # when the machine is busy, which made this test fail on correct spacing.
        for _ in range(3):
            starts.append(limiter.take_turn())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    gaps = [later - earlier for earlier, later in itertools.pairwise(sorted(starts))]
    assert len(starts) == 12
    assert min(gaps) >= 1 / 8 - 0.01
