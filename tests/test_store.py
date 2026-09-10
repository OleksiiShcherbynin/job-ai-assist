from datetime import date, datetime, timezone

import pytest

from app.store import Store, quota_day
from core.models import VacancyCard


def card(offer_id="O5000001", title="Junior Data Engineer", company="ACME") -> VacancyCard:
    return VacancyCard(
        offer_id=offer_id,
        title=title,
        company=company,
        url=f"https://www.profesia.sk/praca/acme/{offer_id}",
    )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "seen.db")


def test_an_unrecorded_vacancy_is_new(store):
    assert store.is_new("O5000001") is True


def test_a_recorded_vacancy_is_no_longer_new(store):
    store.record(card(), status="scored")

    assert store.is_new("O5000001") is False


def test_what_was_recorded_survives_reopening_the_database(tmp_path):
    path = tmp_path / "seen.db"
    Store(path).record(card(), status="scored")

    assert Store(path).is_new("O5000001") is False


def test_the_same_posting_under_a_new_offer_id_is_recognised_as_a_repost(store):
    """Softec posted one vacancy as O5321098 and again as O5350242."""
    store.record(card(offer_id="O5321098", title="Junior IT Analytik (F/M)", company="Softec"), status="scored")

    original = store.find_repost(card(offer_id="O5350242", title="Junior IT Analytik (F/M)", company="Softec"))

    assert original == "O5321098"


def test_a_different_company_with_the_same_title_is_not_a_repost(store):
    store.record(card(offer_id="O5321098", title="Junior Developer", company="Softec"), status="scored")

    assert store.find_repost(card(offer_id="O5350242", title="Junior Developer", company="Anasoft")) is None


def test_a_recorded_verdict_can_be_read_back(store):
    """A repost must inherit the original's score instead of being judged again."""
    store.record(card(), status="scored-final", score=77,
                 reasons=["fits the stack"], reasons_ru=["подходит по стеку"])

    verdict = store.verdict("O5000001")

    assert verdict.score == 77
    assert verdict.reasons == ["fits the stack"]
    assert verdict.reasons_ru == ["подходит по стеку"]
    assert verdict.status == "scored-final"


def test_a_second_verdict_replaces_the_first(store):
    """Observed on a live run: the rough score was recorded, the final judge
    then scored 98 -> 78 with the whole resume in hand, and the store kept the
    rough number. A repost would have inherited the wrong verdict."""
    store.record(card(), status="scored-rough", score=98,
                 reasons=["looks close"], reasons_ru=["выглядит близко"])

    store.record(card(), status="scored-final", score=78,
                 reasons=["first-year student"], reasons_ru=["студент первого курса"])

    verdict = store.verdict("O5000001")
    assert verdict.score == 78
    assert verdict.status == "scored-final"
    assert verdict.reasons == ["first-year student"]
    assert verdict.reasons_ru == ["студент первого курса"]


def test_the_posting_text_is_not_lost_when_a_later_write_omits_it(store):
    """The final judgement carries no detail_text; re-scoring later without
    re-scraping depends on the stored text surviving."""
    store.record(card(), status="scored-rough", score=50, detail_text="the whole posting")

    store.record(card(), status="scored-final", score=78)

    row = store._connection.execute(
        "SELECT detail_text FROM vacancies WHERE offer_id = 'O5000001'"
    ).fetchone()
    assert row["detail_text"] == "the whole posting"


def test_first_seen_survives_a_later_write(store):
    store.record(card(), status="scored-rough", score=50,
                 moment=datetime(2026, 9, 1, tzinfo=timezone.utc))

    store.record(card(), status="scored-final", score=78,
                 moment=datetime(2026, 9, 10, tzinfo=timezone.utc))

    row = store._connection.execute(
        "SELECT first_seen, last_seen FROM vacancies WHERE offer_id = 'O5000001'"
    ).fetchone()
    assert row["first_seen"].startswith("2026-09-01")
    assert row["last_seen"].startswith("2026-09-10")


def test_reading_a_verdict_that_was_never_recorded_gives_nothing(store):
    assert store.verdict("O9999999") is None


def test_no_calls_are_counted_before_any_are_made(store):
    assert store.calls_used("gemini-3.5-flash") == 0


def test_calls_are_counted_per_model(store):
    store.record_call("gemini-3.5-flash")
    store.record_call("gemini-3.5-flash")
    store.record_call("gemini-3.1-flash-lite")

    assert store.calls_used("gemini-3.5-flash") == 2
    assert store.calls_used("gemini-3.1-flash-lite") == 1


def test_the_call_count_belongs_to_a_quota_day_not_a_calendar_day(store):
    yesterday_pacific = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)   # 23:00 Sep 9 in Pacific
    today_pacific = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)       # 01:00 Sep 10 in Pacific

    store.record_call("gemini-3.5-flash", moment=yesterday_pacific)

    assert store.calls_used("gemini-3.5-flash", moment=yesterday_pacific) == 1
    assert store.calls_used("gemini-3.5-flash", moment=today_pacific) == 0


def test_quota_day_follows_pacific_midnight_not_utc():
    """Google resets the daily quota at midnight Pacific, so a Bratislava
    morning run is still spending the previous quota day."""
    assert quota_day(datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)) == date(2026, 9, 9)
    assert quota_day(datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)) == date(2026, 9, 10)


def test_a_fresh_store_has_never_run(store):
    assert store.last_run_date() is None


def test_the_last_run_date_is_remembered(store):
    store.mark_run(date(2026, 9, 10))

    assert store.last_run_date() == date(2026, 9, 10)
