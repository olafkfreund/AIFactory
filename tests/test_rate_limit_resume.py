#!/usr/bin/env python3
"""
Tests for Rate-Limit Auto-Resume (#272)
=======================================

Covers the pure foundation in ``core.error_utils``:
- Rate-limit detection (positive + negative, incl. a normal error).
- Cooldown / reset-time extraction across provider shapes (retry-after header,
  unix-epoch reset, ISO timestamp, SDK "retry in Ns", structured attrs/keys).
- The bounded resume policy with an injected clock/rng: resumes after the
  cooldown, respects max-retries, and gives up past the total-wait cap.
"""

from core.error_utils import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    MAX_RATE_LIMIT_COOLDOWN_SECONDS,
    RateLimitResumePolicy,
    decide_rate_limit_resume,
    extract_rate_limit_cooldown,
    is_rate_limit_error,
)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
class TestRateLimitDetection:
    def test_http_429_is_rate_limit(self):
        assert is_rate_limit_error(Exception("HTTP 429 Too Many Requests"))

    def test_rate_limit_phrase_is_rate_limit(self):
        assert is_rate_limit_error(Exception("Rate limit reached for requests"))

    def test_usage_limit_is_rate_limit(self):
        assert is_rate_limit_error(Exception("usage limit exceeded this week"))

    def test_quota_exceeded_is_rate_limit(self):
        assert is_rate_limit_error(Exception("quota exceeded"))

    def test_normal_error_is_not_rate_limit(self):
        # A plain error must NOT be misclassified as a rate limit.
        assert not is_rate_limit_error(Exception("FileNotFoundError: spec.md"))

    def test_auth_error_is_not_rate_limit(self):
        assert not is_rate_limit_error(Exception("HTTP 401 Unauthorized"))

    def test_429_substring_in_larger_number_is_not_rate_limit(self):
        # Word-boundary guard: "4290" should not match "429".
        assert not is_rate_limit_error(Exception("processed 4290 tokens"))


# ---------------------------------------------------------------------------
# Cooldown / reset-time extraction
# ---------------------------------------------------------------------------
class TestExtractCooldown:
    def test_retry_after_header_seconds(self):
        cd = extract_rate_limit_cooldown("429: retry-after: 30")
        assert cd == 30.0

    def test_retry_after_underscore_key(self):
        cd = extract_rate_limit_cooldown('{"retry_after": 45}')
        assert cd == 45.0

    def test_sdk_retry_in_seconds(self):
        cd = extract_rate_limit_cooldown("Rate limit event (retry in 12s)")
        assert cd == 12.0

    def test_retry_in_seconds_word(self):
        cd = extract_rate_limit_cooldown("please retry in 8 seconds")
        assert cd == 8.0

    def test_structured_attribute_on_exception(self):
        class RL(Exception):
            retry_after = 90

        assert extract_rate_limit_cooldown(RL("rate limited")) == 90.0

    def test_structured_retry_after_seconds_attr(self):
        class RL(Exception):
            retry_after_seconds = 17

        assert extract_rate_limit_cooldown(RL("rate limited")) == 17.0

    def test_dict_retry_after_key(self):
        assert extract_rate_limit_cooldown({"retry_after": 25}) == 25.0

    def test_unix_epoch_reset(self):
        # Use a realistic 10-digit unix epoch so the reset is recognised as
        # absolute (the parser requires >= 9 digits to avoid matching counts).
        now = 1_780_401_600.0  # 2026-06-02T12:00:00Z
        text = f"x-ratelimit-reset: {int(now + 120)}"
        cd = extract_rate_limit_cooldown(text, now=now)
        assert abs(cd - 120.0) < 1.0

    def test_iso_reset_timestamp(self):
        # now = 2026-06-02T12:00:00Z; reset 5 min later.
        now = 1_780_401_600.0  # epoch for 2026-06-02T12:00:00Z
        text = "rate limited; resets_at: 2026-06-02T12:05:00Z"
        cd = extract_rate_limit_cooldown(text, now=now)
        assert abs(cd - 300.0) < 2.0

    def test_default_when_no_timing(self):
        cd = extract_rate_limit_cooldown("Rate limit reached")
        assert cd == DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS

    def test_custom_default(self):
        cd = extract_rate_limit_cooldown("rate limit", default_cooldown=7.0)
        assert cd == 7.0

    def test_cooldown_clamped_to_max(self):
        cd = extract_rate_limit_cooldown("retry-after: 999999")
        assert cd == MAX_RATE_LIMIT_COOLDOWN_SECONDS

    def test_past_reset_yields_zero(self):
        now = 1_780_401_600.0  # 2026-06-02T12:00:00Z
        # Reset epoch 100s in the past relative to now.
        text = f"x-ratelimit-reset: {int(now - 100)}"
        cd = extract_rate_limit_cooldown(text, now=now)
        assert cd == 0.0


# ---------------------------------------------------------------------------
# Resume policy
# ---------------------------------------------------------------------------
class TestResumePolicy:
    def _no_jitter(self):
        # rng() -> 0.0 makes jitter deterministic (zero) for exact assertions.
        return 0.0

    def test_resumes_after_cooldown(self):
        decision = decide_rate_limit_resume(
            cooldown_seconds=30.0,
            attempt=1,
            elapsed_wait_seconds=0.0,
            rng=self._no_jitter,
        )
        assert decision.should_resume is True
        assert decision.wait_seconds == 30.0

    def test_jitter_added_within_ratio(self):
        policy = RateLimitResumePolicy(jitter_ratio=0.1)
        # rng -> 1.0 gives the max jitter: 30 * 0.1 * 1.0 = 3.0
        decision = decide_rate_limit_resume(
            cooldown_seconds=30.0,
            attempt=1,
            elapsed_wait_seconds=0.0,
            policy=policy,
            rng=lambda: 1.0,
        )
        assert decision.should_resume is True
        assert decision.wait_seconds == 33.0

    def test_min_cooldown_floor_prevents_busy_loop(self):
        policy = RateLimitResumePolicy(min_cooldown_seconds=5.0)
        decision = decide_rate_limit_resume(
            cooldown_seconds=0.0,  # provider said retry_after: 0
            attempt=1,
            elapsed_wait_seconds=0.0,
            policy=policy,
            rng=self._no_jitter,
        )
        assert decision.should_resume is True
        assert decision.wait_seconds == 5.0

    def test_respects_max_retries(self):
        policy = RateLimitResumePolicy(max_retries=3)
        # attempt 3 (== cap) still resumes
        ok = decide_rate_limit_resume(
            cooldown_seconds=10.0,
            attempt=3,
            elapsed_wait_seconds=0.0,
            policy=policy,
            rng=self._no_jitter,
        )
        assert ok.should_resume is True
        # attempt 4 (> cap) gives up
        give_up = decide_rate_limit_resume(
            cooldown_seconds=10.0,
            attempt=4,
            elapsed_wait_seconds=0.0,
            policy=policy,
            rng=self._no_jitter,
        )
        assert give_up.should_resume is False
        assert give_up.wait_seconds == 0.0
        assert "max retries" in give_up.reason

    def test_respects_total_wait_cap(self):
        policy = RateLimitResumePolicy(max_retries=100, max_total_wait_seconds=100.0)
        # Already waited 80s; a 30s cooldown would push us to 110 > 100 -> stop.
        decision = decide_rate_limit_resume(
            cooldown_seconds=30.0,
            attempt=2,
            elapsed_wait_seconds=80.0,
            policy=policy,
            rng=self._no_jitter,
        )
        assert decision.should_resume is False
        assert decision.wait_seconds == 0.0
        assert "total wait" in decision.reason

    def test_total_wait_cap_allows_within_budget(self):
        policy = RateLimitResumePolicy(max_retries=100, max_total_wait_seconds=100.0)
        decision = decide_rate_limit_resume(
            cooldown_seconds=10.0,
            attempt=2,
            elapsed_wait_seconds=80.0,
            policy=policy,
            rng=self._no_jitter,
        )
        assert decision.should_resume is True
        assert decision.wait_seconds == 10.0

    def test_simulated_resume_loop_gives_up_at_cap(self):
        """Integration-style: loop until the policy stops us, with a fake clock."""
        policy = RateLimitResumePolicy(max_retries=10, max_total_wait_seconds=50.0)
        elapsed = 0.0
        attempts = 0
        clock = 0.0  # injected "now" advanced by each wait
        while True:
            attempts += 1
            cd = extract_rate_limit_cooldown("rate limit", default_cooldown=20.0)
            decision = decide_rate_limit_resume(
                cooldown_seconds=cd,
                attempt=attempts,
                elapsed_wait_seconds=elapsed,
                policy=policy,
                rng=self._no_jitter,
            )
            if not decision.should_resume:
                break
            elapsed += decision.wait_seconds
            clock += decision.wait_seconds
        # 20 + 20 = 40 <= 50, third would be 60 > 50 -> stop after 2 waits.
        assert elapsed == 40.0
        assert attempts == 3
        assert clock == 40.0
