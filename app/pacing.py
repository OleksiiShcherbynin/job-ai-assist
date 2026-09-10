"""Stay under the Gemini rate limits instead of discovering them via 429.

Three limits apply at once — per minute, per day, and tokens per minute. The
first two are the ones a nightly run hits, and they need opposite responses: a
minute limit clears by waiting a few seconds, a daily one does not clear until
midnight Pacific. Treating both as "retry with backoff" burns the remaining
quota on retries that cannot succeed.
"""

import time
from collections import defaultdict, deque
from enum import Enum
from typing import Callable

from app.config import RunConfig
from app.store import Store
from local_connectors.llm import is_account_error

_MINUTE = 60.0


class RateLimitKind(Enum):
    TRANSIENT = "transient"
    """Per-minute or burst limit: waiting clears it."""

    DAILY = "daily"
    """Daily quota: nothing clears it before midnight Pacific."""

    ACCOUNT = "account"
    """Billing, not throughput: no model will answer and waiting will not help."""

    OTHER = "other"
    """Not a rate limit at all."""


class DailyQuotaExhausted(RuntimeError):
    def __init__(self, model: str, used: int, limit: int) -> None:
        super().__init__(f"{model}: {used} of {limit} daily calls already spent")
        self.model = model


class AccountBlocked(RuntimeError):
    """The API refuses everything for a reason no retry can fix."""


def classify_rate_limit(error: Exception) -> RateLimitKind:
    """Tell a spent day from a busy minute from a dead account.

    The Gemini API reports 429 under three codes — rate_limit_exceeded and
    too_many_requests for the per-minute and burst limits, quota_exceeded for
    the daily one — and also uses 429 for depleted billing credits, which is
    neither and must not be retried.
    """
    if is_account_error(error):
        return RateLimitKind.ACCOUNT

    text = str(error).lower()
    if "quota_exceeded" in text:
        return RateLimitKind.DAILY
    if "rate_limit_exceeded" in text or "too_many_requests" in text:
        return RateLimitKind.TRANSIENT
    return RateLimitKind.OTHER


class Pacer:
    def __init__(
        self,
        store: Store,
        config: RunConfig,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def wait_for_slot(self, model: str) -> None:
        """Block until this model may be called. Raises if its day is spent."""
        quota = self._config.quota_for(model)

        used_today = self._store.calls_used(model)
        if used_today >= quota.rpd:
            # Waiting this one out would mean sleeping until midnight Pacific.
            raise DailyQuotaExhausted(model, used_today, quota.rpd)

        window = self._recent[model]
        self._forget_old(window)
        if len(window) >= quota.rpm:
            self._sleep(max(0.0, window[0] + _MINUTE - self._clock()))
            self._forget_old(window)

    def record(self, model: str) -> None:
        self._recent[model].append(self._clock())
        self._store.record_call(model)

    def _forget_old(self, window: deque[float]) -> None:
        cutoff = self._clock() - _MINUTE
        while window and window[0] <= cutoff:
            window.popleft()
