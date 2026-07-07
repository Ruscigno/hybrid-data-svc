"""Tests for data_svc/providers/yahoo/ratelimit.py — pure, no network, injectable clock/sleep."""

import pytest

from data_svc.providers.yahoo.ratelimit import RateLimiter, AdaptiveRateLimiter


class TestRateLimiter:
    def test_acquire_60rpm_sleeps_approximately_one_second_between_calls(self):
        """RateLimiter(rpm=60): 3 acquire() calls should sleep ~1s between each."""
        clock = [0.0]
        slept = []

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs  # advance clock by the sleep amount

        limiter = RateLimiter(rpm=60, monotonic=fake_monotonic)

        # First acquire — no prior call so no sleep needed
        limiter.acquire(sleep=fake_sleep)
        # Second acquire — should sleep ~1s (60/60 = 1.0s interval)
        clock[0] += 0.001  # tiny wall-clock advancement (instant response)
        limiter.acquire(sleep=fake_sleep)
        clock[0] += 0.001
        limiter.acquire(sleep=fake_sleep)

        # The sleeps should be close to 1.0s (allowing for tiny clock advancement)
        timed_sleeps = [s for s in slept if s > 0]
        assert len(timed_sleeps) >= 2, f"Expected >=2 positive sleeps, got {slept}"
        for s in timed_sleeps:
            assert 0.9 <= s <= 1.1, f"Sleep {s} not near 1.0s"

    def test_acquire_total_sleep_covers_two_intervals(self):
        """3 calls at rpm=60 sleep a total of ~2s (2 intervals)."""
        clock = [0.0]
        slept = []

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter = RateLimiter(rpm=60, monotonic=fake_monotonic)

        limiter.acquire(sleep=fake_sleep)
        clock[0] += 0.001
        limiter.acquire(sleep=fake_sleep)
        clock[0] += 0.001
        limiter.acquire(sleep=fake_sleep)

        total = sum(slept)
        assert total >= 1.9, f"Total sleep {total} less than ~2s"

    def test_rpm_property_returns_initial_value(self):
        limiter = RateLimiter(rpm=30)
        assert limiter.rpm == 30.0

    def test_set_rpm_changes_pacing(self):
        limiter = RateLimiter(rpm=60)
        limiter.set_rpm(30)
        assert limiter.rpm == 30.0

    def test_first_acquire_does_not_sleep(self):
        """First acquire() call should not sleep (no prior call to pace against)."""
        clock = [0.0]
        slept = []

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter = RateLimiter(rpm=60, monotonic=fake_monotonic)
        limiter.acquire(sleep=fake_sleep)

        positive_sleeps = [s for s in slept if s > 0]
        assert len(positive_sleeps) == 0, f"First acquire should not sleep, got {slept}"


class TestAdaptiveRateLimiterOn429:
    def test_on_429_reduces_rpm_by_75_percent(self):
        """on_429() -> rpm = rpm * 0.75."""
        limiter = AdaptiveRateLimiter(rpm=60)
        limiter.on_429()
        assert limiter.rpm == 45.0

    def test_repeated_on_429_floors_at_floor_rpm(self):
        """Repeated on_429() floors at floor_rpm (default 5.0)."""
        limiter = AdaptiveRateLimiter(rpm=60, floor_rpm=5.0)
        # Throttle many times until floor
        for _ in range(20):
            limiter.on_429()
        assert limiter.rpm == 5.0

    def test_on_429_resets_success_streak(self):
        """on_429() resets the success streak counter."""
        limiter = AdaptiveRateLimiter(rpm=60, restore_after=30)
        # Build up some successes
        for _ in range(15):
            limiter.on_success()
        # 429 should reset the streak
        limiter.on_429()
        # Now need another full restore_after successes before restoring
        # (rpm was reduced, so restore would only happen after 30 more)
        initial_rpm_after_429 = limiter.rpm
        for _ in range(29):
            limiter.on_success()
        # Not yet restored after only 29
        assert limiter.rpm == initial_rpm_after_429


class TestAdaptiveRateLimiterOnSuccess:
    def test_on_success_restores_rpm_after_restore_after_calls(self):
        """30 on_success() calls after throttle -> rpm increases by restore_step."""
        limiter = AdaptiveRateLimiter(rpm=60, restore_step=5.0, restore_after=30)
        limiter.on_429()  # rpm -> 45
        throttled_rpm = limiter.rpm  # 45.0

        for _ in range(30):
            limiter.on_success()

        assert limiter.rpm == throttled_rpm + 5.0  # 50.0

    def test_on_success_never_restores_above_base_rpm(self):
        """on_success() never raises rpm above the initial base."""
        limiter = AdaptiveRateLimiter(rpm=60, restore_step=5.0, restore_after=30)
        # Don't throttle — just call success many times
        for _ in range(300):
            limiter.on_success()
        assert limiter.rpm == 60.0

    def test_on_success_resets_streak_after_restore(self):
        """After restore, streak resets (need another restore_after for next bump)."""
        limiter = AdaptiveRateLimiter(rpm=60, restore_step=5.0, restore_after=30)
        limiter.on_429()  # rpm -> 45
        limiter.on_429()  # rpm -> 33.75

        rpm_after_two_429s = limiter.rpm

        # First 30 successes -> first restore
        for _ in range(30):
            limiter.on_success()
        rpm_after_first_restore = limiter.rpm
        assert rpm_after_first_restore == rpm_after_two_429s + 5.0

        # Next 29 successes -> NOT yet restored again
        for _ in range(29):
            limiter.on_success()
        assert limiter.rpm == rpm_after_first_restore  # unchanged

        # 30th success -> second restore
        limiter.on_success()
        assert limiter.rpm == rpm_after_first_restore + 5.0

    def test_acquire_delegates_to_inner_limiter(self):
        """AdaptiveRateLimiter.acquire() actually paces (delegates to RateLimiter)."""
        clock = [0.0]
        slept = []

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter = AdaptiveRateLimiter(rpm=60, monotonic=fake_monotonic)

        limiter.acquire(sleep=fake_sleep)
        clock[0] += 0.001
        limiter.acquire(sleep=fake_sleep)

        positive_sleeps = [s for s in slept if s > 0]
        assert len(positive_sleeps) >= 1


class TestAdaptiveRateLimiterNotifyRetryAfter:
    """R1: notify_retry_after() records a cooldown that acquire() honors."""

    def test_notify_retry_after_causes_cooldown_sleep(self):
        """notify_retry_after(30) -> next acquire() sleeps >= 30s."""
        clock = [0.0]
        slept = []

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter = AdaptiveRateLimiter(rpm=60, monotonic=fake_monotonic)
        limiter.notify_retry_after(30.0)

        # acquire() should first sleep the cooldown, then pacing (first call = no pacing)
        limiter.acquire(sleep=fake_sleep)

        assert any(s >= 30.0 for s in slept), f"Expected cooldown sleep >=30s, got {slept}"

    def test_notify_retry_after_cooldown_not_double_applied(self):
        """Once cooldown expires (after first acquire), second acquire does not re-sleep it."""
        clock = [0.0]
        slept = []

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter = AdaptiveRateLimiter(rpm=60, monotonic=fake_monotonic)
        limiter.notify_retry_after(30.0)

        # First acquire sleeps the cooldown
        limiter.acquire(sleep=fake_sleep)
        slept_first = list(slept)

        # Advance clock a bit (simulate time passing)
        clock[0] += 0.001
        slept.clear()

        # Second acquire: cooldown is already past, should only sleep normal pacing (~1s at 60rpm)
        limiter.acquire(sleep=fake_sleep)

        # No sleep >= 30 in second acquire
        assert not any(s >= 30.0 for s in slept), (
            f"Cooldown was re-applied on second acquire: {slept}"
        )

    def test_notify_retry_after_takes_max_of_multiple_calls(self):
        """Multiple notify_retry_after calls — the longer one governs."""
        clock = [0.0]
        slept = []

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter = AdaptiveRateLimiter(rpm=60, monotonic=fake_monotonic)
        limiter.notify_retry_after(10.0)
        limiter.notify_retry_after(45.0)
        limiter.notify_retry_after(20.0)  # should not override 45

        limiter.acquire(sleep=fake_sleep)

        assert any(s >= 45.0 for s in slept), (
            f"Expected cooldown >= 45s (max of 10/45/20), got {slept}"
        )
