"""In-process rate limiter with D12 adaptive 429 auto-throttle.

D12 algorithm:
- On 429: rpm = max(floor_rpm, rpm * throttle_factor);  success streak resets.
- On success: streak += 1; every restore_after clean successes,
              rpm = min(base_rpm, rpm + restore_step);  streak resets.
"""

from __future__ import annotations

import time
from typing import Callable


class RateLimiter:
    """Even-paces requests to ``rpm`` per minute using a token-bucket style interval.

    ``monotonic`` and ``sleep`` are injectable for deterministic testing.
    """

    def __init__(
        self,
        rpm: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rpm = float(rpm)
        self._monotonic = monotonic
        self._last_call: float | None = None

    @property
    def rpm(self) -> float:
        return self._rpm

    def set_rpm(self, rpm: float) -> None:
        self._rpm = float(rpm)

    def acquire(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        """Block (via ``sleep``) until the next request slot is available."""
        now = self._monotonic()
        if self._last_call is not None:
            interval = 60.0 / self._rpm
            elapsed = now - self._last_call
            wait = interval - elapsed
            if wait > 0:
                sleep(wait)
        self._last_call = self._monotonic()


class AdaptiveRateLimiter:
    """Wraps :class:`RateLimiter` with D12 adaptive 429 auto-throttle.

    Parameters
    ----------
    rpm:            Initial (and maximum) requests-per-minute.
    floor_rpm:      Minimum rpm — throttle never goes below this.
    throttle_factor: Multiply current rpm by this on each 429 (default 0.75).
    restore_step:   Add this many rpm per ``restore_after`` clean successes.
    restore_after:  Number of consecutive successes before restoring rpm.
    monotonic:      Injectable clock for testing.
    """

    def __init__(
        self,
        rpm: float,
        *,
        floor_rpm: float = 5.0,
        throttle_factor: float = 0.75,
        restore_step: float = 5.0,
        restore_after: int = 30,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_rpm = float(rpm)
        self._floor_rpm = float(floor_rpm)
        self._throttle_factor = throttle_factor
        self._restore_step = float(restore_step)
        self._restore_after = restore_after
        self._monotonic = monotonic
        self._limiter = RateLimiter(rpm, monotonic=monotonic)
        self._streak: int = 0
        self._cooldown_until: float = 0.0

    @property
    def rpm(self) -> float:
        return self._limiter.rpm

    def notify_retry_after(self, seconds: float) -> None:
        """Record a Retry-After cooldown.

        The next ``acquire()`` will sleep until the cooldown expires before
        applying its normal pacing interval.
        """
        self._cooldown_until = max(
            self._cooldown_until, self._monotonic() + seconds
        )

    def acquire(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        """Wait out any active Retry-After cooldown, then delegate to inner limiter."""
        now = self._monotonic()
        remaining = self._cooldown_until - now
        if remaining > 0:
            sleep(remaining)
        self._limiter.acquire(sleep=sleep)

    def on_429(self) -> None:
        """Throttle: rpm = max(floor_rpm, rpm * throttle_factor); reset streak."""
        new_rpm = max(self._floor_rpm, self._limiter.rpm * self._throttle_factor)
        self._limiter.set_rpm(new_rpm)
        self._streak = 0

    def on_success(self) -> None:
        """Record a clean success; restore rpm after every restore_after streak."""
        self._streak += 1
        if self._streak >= self._restore_after:
            new_rpm = min(self._base_rpm, self._limiter.rpm + self._restore_step)
            self._limiter.set_rpm(new_rpm)
            self._streak = 0
