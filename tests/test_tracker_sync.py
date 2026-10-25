import logging
from datetime import datetime, timezone

import pytest

from app.store import Store
from app.tracker_sync import sync, to_payload
from core.models import VacancyCard
from local_connectors.tracker import TrackerError

SEEN = datetime(2026, 10, 20, 8, 14, tzinfo=timezone.utc)


def card(offer_id="O1", title="Junior Dev", company="Softec") -> VacancyCard:
    return VacancyCard(
        offer_id=offer_id,
        title=title,
        company=company,
        url=f"https://www.profesia.sk/praca/softec/{offer_id}",
        location="Bratislava",
        salary_text="1 800 EUR/month",
    )


class FakeTracker:
    """Records every batch; answers each item with what `answer` says."""

    def __init__(self, answer=None, fail=False):
        self.batches: list[list[dict]] = []
        self.answer = answer or (lambda item: {"status": "created", "id": 1})
        self.fail = fail

    def push(self, vacancies):
        self.batches.append(vacancies)
        if self.fail:
            raise TrackerError("connection refused")
        return [{"external_id": item["external_id"], **self.answer(item)} for item in vacancies]


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "seen.db")


def queued(store):
    return [row["offer_id"] for row in store.tracker_pending(1000)]


def test_payload_has_the_contract_fields(store):
    store.record(card(), status="scored-final", score=78,
                 reasons=["Python matches"], reasons_ru=["Совпадает Python"], moment=SEEN)
    assert to_payload(store.tracker_pending(1)[0]) == {
        "external_id": "O1",
        "title": "Junior Dev",
        "company": "Softec",
        "url": "https://www.profesia.sk/praca/softec/O1",
        "location": "Bratislava",
        "salary": "1 800 EUR/month",
        "score": 78,
        "score_stage": "final",
        "reasons_en": ["Python matches"],
        "reasons_ru": ["Совпадает Python"],
        "first_seen_at": SEEN.isoformat(),
    }


def test_a_rough_score_is_sent_as_rough(store):
    store.record(card(), status="scored-rough", score=50)
    assert to_payload(store.tracker_pending(1)[0])["score_stage"] == "rough"


def test_a_repost_is_sent_with_its_originals_stage(store):
    store.record(card(offer_id="O1"), status="scored-final", score=80)
    store.record(card(offer_id="O2"), status="repost-of-O1", score=80)
    rows = {row["offer_id"]: row for row in store.tracker_pending(10)}
    assert to_payload(rows["O2"])["score_stage"] == "final"


def test_blank_reasons_are_dropped(store):
    store.record(card(), status="scored-rough", score=50, reasons=["ok", "  "], reasons_ru=[""])
    payload = to_payload(store.tracker_pending(1)[0])
    assert (payload["reasons_en"], payload["reasons_ru"]) == (["ok"], [])


def test_a_missing_company_is_sent_empty(store):
    store.record(card(company=None), status="scored-rough", score=50)
    assert to_payload(store.tracker_pending(1)[0])["company"] == ""


def test_accepted_items_leave_the_queue(store):
    store.record(card(offer_id="O1"), status="scored-rough", score=50)
    store.record(card(offer_id="O2"), status="scored-rough", score=50)
    answers = iter([{"status": "created", "id": 1}, {"status": "updated", "id": 2}])
    result = sync(store, FakeTracker(answer=lambda item: next(answers)))
    assert (result.sent, result.accepted, result.invalid) == (2, 2, 0)
    assert queued(store) == []


def test_invalid_items_are_logged_and_not_retried(store, caplog):
    store.record(card(offer_id="O1"), status="scored-rough", score=50)
    tracker = FakeTracker(answer=lambda item: {"status": "invalid", "errors": {"score": ["bad"]}})
    with caplog.at_level(logging.WARNING):
        result = sync(store, tracker)
    assert result.invalid == 1
    assert queued(store) == []
    assert "O1" in caplog.text and "score" in caplog.text


def test_a_failed_push_keeps_everything_queued(store):
    store.record(card(offer_id="O1"), status="scored-rough", score=50)
    with pytest.raises(TrackerError):
        sync(store, FakeTracker(fail=True))
    assert queued(store) == ["O1"]


def test_a_large_backlog_goes_out_in_batches_of_200(store):
    # Review Focus #1: days of downtime drain in batches, nothing lost.
    for n in range(450):
        store.record(card(offer_id=f"O{n:04d}"), status="scored-rough", score=50)
    tracker = FakeTracker()
    result = sync(store, tracker)
    assert [len(batch) for batch in tracker.batches] == [200, 200, 50]
    assert result.accepted == 450
    assert queued(store) == []


def test_a_short_answer_leaves_unmatched_items_queued(store):
    # Review Focus #5: a malformed answer must not mark unsent items delivered.
    store.record(card(offer_id="O1"), status="scored-rough", score=50)
    store.record(card(offer_id="O2"), status="scored-rough", score=50)

    class ShortTracker(FakeTracker):
        def push(self, vacancies):
            self.batches.append(vacancies)
            return [{"external_id": vacancies[0]["external_id"], "status": "created", "id": 1}]

    tracker = ShortTracker()
    result = sync(store, tracker)
    assert result.accepted == 1
    assert len(tracker.batches) == 1  # stops instead of resending the same rows
    assert len(queued(store)) == 1
