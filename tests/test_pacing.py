import pytest

from app.config import ModelQuota, RunConfig
from app.pacing import DailyQuotaExhausted, Pacer, RateLimitKind, classify_rate_limit
from app.store import Store
from core.models import SearchPreferences

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


@pytest.mark.parametrize(
    "message, kind",
    [
        ("429 rate_limit_exceeded: per-minute request limit", RateLimitKind.TRANSIENT),
        ("429 too_many_requests", RateLimitKind.TRANSIENT),
        ("429 quota_exceeded: daily quota reached", RateLimitKind.DAILY),
        ("503 UNAVAILABLE", RateLimitKind.OTHER),
    ],
)
def test_a_429_is_classified_by_which_quota_ran_out(message, kind):
    """Google's own error page separates the per-minute codes from the daily
    one, so a minute-limit hit must not be mistaken for a spent day."""
    assert classify_rate_limit(Exception(message)) is kind
