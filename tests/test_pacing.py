import pytest

from app.config import ModelQuota, RunConfig
from app.pacing import DailyQuotaExhausted, Pacer, RateLimitKind, classify_rate_limit
from app.store import Store
from core.models import SearchPreferences
from local_connectors.llm import is_account_error

MODEL = "gemini-3.5-flash"


class FakeClock:
    """Time only moves when the code under test sleeps, so a test that would
    otherwise wait a minute finishes instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def config_with(rpm: int, rpd: int, tmp_path) -> RunConfig:
    return RunConfig(
        resume_path=tmp_path / "cv.pdf",
        state_path=tmp_path / "seen.db",
        report_dir=tmp_path / "reports",
        list_url="https://example.invalid/",
        start_page=1,
        stop_page=1,
        call_budget=100,
        min_score=0,
        final_judge_limit=12,
        preferences=SearchPreferences(desired_roles=[]),
        quotas={MODEL: ModelQuota(rpm=rpm, rpd=rpd)},
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_pacer(tmp_path, clock, rpm=10, rpd=100) -> Pacer:
    store = Store(tmp_path / "seen.db")
    return Pacer(store, config_with(rpm, rpd, tmp_path), clock=clock.time, sleeper=clock.sleep)


def test_a_call_under_both_limits_does_not_wait(tmp_path, clock):
    pacer = make_pacer(tmp_path, clock)

    pacer.wait_for_slot(MODEL)

    assert clock.slept == []


def test_the_minute_limit_makes_the_next_call_wait(tmp_path, clock):
    pacer = make_pacer(tmp_path, clock, rpm=2)
    for _ in range(2):
        pacer.wait_for_slot(MODEL)
        pacer.record(MODEL)

    pacer.wait_for_slot(MODEL)

    assert clock.slept, "a third call within the minute should have waited"
    assert 0 < sum(clock.slept) <= 61


def test_the_minute_window_slides_rather_than_resetting(tmp_path, clock):
    pacer = make_pacer(tmp_path, clock, rpm=2)
    for _ in range(2):
        pacer.wait_for_slot(MODEL)
        pacer.record(MODEL)
    clock.now += 61  # the earlier calls have aged out

    pacer.wait_for_slot(MODEL)

    assert clock.slept == []


def test_the_daily_limit_stops_the_model_instead_of_waiting(tmp_path, clock):
    """Waiting out a daily quota would mean sleeping until Pacific midnight."""
    pacer = make_pacer(tmp_path, clock, rpd=2)
    for _ in range(2):
        pacer.record(MODEL)

    with pytest.raises(DailyQuotaExhausted):
        pacer.wait_for_slot(MODEL)


def test_spent_calls_are_remembered_across_restarts(tmp_path, clock):
    make_pacer(tmp_path, clock, rpd=2).record(MODEL)

    revived = make_pacer(tmp_path, clock, rpd=2)
    revived.record(MODEL)

    with pytest.raises(DailyQuotaExhausted):
        revived.wait_for_slot(MODEL)


def test_the_daily_limit_is_tracked_per_model(tmp_path, clock):
    pacer = make_pacer(tmp_path, clock, rpd=1)
    pacer.record(MODEL)

    pacer.wait_for_slot("gemini-3.1-flash-lite")  # a different model, untouched


DEPLETED = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Your prepayment "
    "credits are depleted. Please go to AI Studio at https://ai.studio/projects to "
    "manage your project and billing.', 'status': 'RESOURCE_EXHAUSTED'}}"
)


def _google_429(quota_id: str, limit: int, retry_delay: str) -> str:
    """Google's real wording, from the container log of 2026-09-23.

    Note "plan and billing details": the same sentence arrives for a limit that
    clears in a minute and for one that does not clear at all.
    """
    return (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
        "current quota, please check your plan and billing details. For more information "
        "on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.\\n* "
        "Quota exceeded for metric: generativelanguage.googleapis.com/"
        f"generate_content_free_tier_requests, limit: {limit}, model: gemini-3.5-flash "
        f"Please retry in {retry_delay}.', 'status': 'RESOURCE_EXHAUSTED', 'details': "
        "[{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': "
        f"[{{'quotaId': '{quota_id}', 'quotaValue': '{limit}'}}]}}, "
        "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': "
        f"'{retry_delay}'}}]}}}}"
    )


PER_MINUTE = _google_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 5, "51s")
PER_DAY = _google_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 20, "3600s")


@pytest.mark.parametrize(
    "message, kind",
    [
        ("429 rate_limit_exceeded: per-minute request limit", RateLimitKind.TRANSIENT),
        ("429 too_many_requests", RateLimitKind.TRANSIENT),
        ("429 quota_exceeded: daily quota reached", RateLimitKind.DAILY),
        (PER_MINUTE, RateLimitKind.TRANSIENT),
        (PER_DAY, RateLimitKind.DAILY),
        (DEPLETED, RateLimitKind.ACCOUNT),
        ("503 UNAVAILABLE", RateLimitKind.OTHER),
    ],
)
def test_a_429_is_classified_by_what_actually_ran_out(message, kind):
    """Google's own error page separates the per-minute codes from the daily one.
    Depleted billing credits arrive as 429 too, and mean something else again:
    no model will answer and no amount of waiting will help."""
    assert classify_rate_limit(Exception(message)) is kind


def test_depleted_credits_are_not_mistaken_for_a_rate_limit():
    """Observed on a live run: every model returned this, and the retry logic
    turned 20 vacancies into 240 futile requests."""
    assert classify_rate_limit(Exception(DEPLETED)) is not RateLimitKind.TRANSIENT
    assert classify_rate_limit(Exception(DEPLETED)) is not RateLimitKind.DAILY


def test_a_busy_minute_is_not_mistaken_for_a_dead_account():
    """The 2026-09-23 run died on this: the word "billing" in Google's ordinary
    rate-limit message made a 51-second wait look like a dead account, and the
    whole run was abandoned with 56 vacancies already scored."""
    assert is_account_error(Exception(PER_MINUTE)) is False
    assert classify_rate_limit(Exception(PER_MINUTE)) is RateLimitKind.TRANSIENT


def test_a_spent_day_is_not_mistaken_for_a_dead_account():
    assert is_account_error(Exception(PER_DAY)) is False


def test_anything_carrying_a_retry_delay_is_a_throughput_limit():
    """A limit that names the seconds to wait is, by construction, one that waiting clears."""
    invented = "429 something new nobody has seen, 'retryDelay': '12s', please check your billing details"

    assert is_account_error(Exception(invented)) is False
