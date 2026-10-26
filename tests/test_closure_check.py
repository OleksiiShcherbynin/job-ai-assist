import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.closure_check import (MAX_FAILURES_IN_A_ROW, MAX_FETCHES, PAUSE_SECONDS, CheckResult,
                               check_closures)
from app.store import Store
from local_connectors.tracker import TrackerError

FIXTURES = Path(__file__).parent / "fixtures"
CLOSED = (FIXTURES / "profesia_posting_closed.html").read_text(encoding="utf-8")
OPEN = (FIXTURES / "profesia_posting_open.html").read_text(encoding="utf-8")
CAPTCHA = "<html><body>Please verify you are human</body></html>"

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 8, 20, tzinfo=timezone.utc)


def url(offer_id: str) -> str:
    return f"https://www.profesia.sk/praca/acme/{offer_id}"


def offer(offer_id: str, link: str | None = None) -> dict:
    return {"external_id": offer_id, "url": link or url(offer_id)}


class FakeTracker:
    """Hands out `offers`; answers every reported item with `answer(item)`."""

    def __init__(self, offers=(), answer=None, fail_get=False, fail_post=False):
        self.offers = list(offers)
        self.answer = answer or (lambda item: "closed")
        self.fail_get, self.fail_post = fail_get, fail_post
        self.gets = 0
        self.reports: list[list[dict]] = []

    def push(self, vacancies):
        return []

    def offers_to_check(self):
        self.gets += 1
        if self.fail_get:
            raise TrackerError("ConnectError: refused")
        return self.offers

    def report_closed(self, closed):
        self.reports.append(closed)
        if self.fail_post:
            raise TrackerError("503 Service Unavailable", status=503)
        return [{"external_id": item["external_id"], "status": self.answer(item)} for item in closed]


class FakeSource:
    """Serves `pages` (url -> (status, html) or an exception) and records the order."""

    def __init__(self, pages):
        self.pages = pages
        self.fetched: list[str] = []

    def fetch_cards(self):
        return []

    def fetch_detail(self, url):
        raise AssertionError("the check reads raw pages only")

    def fetch_page(self, url):
        self.fetched.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "seen.db")


def run(store, tracker, source, sleeps=None, today=TODAY):
    sleeps = [] if sleeps is None else sleeps
    return check_closures(store, tracker, source, today, sleeper=sleeps.append, now=lambda: NOW)


def pending(store):
    return [row["offer_id"] for row in store.closed_pending(1000)]


def test_closed_postings_are_reported_with_seen_at(store):
    tracker = FakeTracker([offer("O1"), offer("O2"), offer("O3")])
    source = FakeSource({url("O1"): (200, CLOSED), url("O2"): (200, OPEN), url("O3"): (404, "gone")})

    result = run(store, tracker, source)

    assert tracker.reports == [[{"external_id": "O1", "seen_at": NOW.isoformat()}]]
    assert (result.listed, result.fetched, result.closed, result.skipped, result.reported) == (3, 3, 1, 1, 1)
    assert pending(store) == []


def test_the_day_is_marked_only_after_the_walk(store):
    run(store, FakeTracker([offer("O1")]), FakeSource({url("O1"): (200, OPEN)}))

    assert store.closure_checked_on() == TODAY


def test_a_failed_to_check_leaves_the_day_unmarked(store):
    # Review Focus #2: the tracker container may start after this one.
    source = FakeSource({})

    with pytest.raises(TrackerError):
        run(store, FakeTracker(fail_get=True), source)

    assert store.closure_checked_on() is None
    assert source.fetched == []


def test_a_failed_report_is_retried_later_without_refetching(store):
    # Review Focus #3: what was found survives a refusing tracker; Profesia is walked once.
    first = FakeSource({url("O1"): (200, CLOSED)})
    with pytest.raises(TrackerError):
        run(store, FakeTracker([offer("O1")], fail_post=True), first)
    assert store.closure_checked_on() == TODAY
    assert pending(store) == ["O1"]

    tracker, second = FakeTracker([offer("O1")]), FakeSource({})
    result = run(store, tracker, second)

    assert second.fetched == []
    assert tracker.gets == 0
    assert tracker.reports == [[{"external_id": "O1", "seen_at": NOW.isoformat()}]]
    assert result.settled == 1
    assert pending(store) == []


def test_a_second_call_the_same_day_does_nothing(store):
    run(store, FakeTracker([offer("O1")]), FakeSource({url("O1"): (200, OPEN)}))
    tracker, source = FakeTracker([offer("O1")]), FakeSource({url("O1"): (200, OPEN)})

    run(store, tracker, source)

    assert (tracker.gets, source.fetched, tracker.reports) == (0, [], [])


def test_a_new_day_walks_again(store):
    run(store, FakeTracker([offer("O1")]), FakeSource({url("O1"): (200, OPEN)}))
    source = FakeSource({url("O1"): (200, CLOSED)})

    run(store, FakeTracker([offer("O1")]), source, today=date(2026, 10, 28))

    assert source.fetched == [url("O1")]


def test_non_profesia_urls_are_skipped_without_a_request(store):
    # Review Focus #4: hand-typed applications point elsewhere; lookalike hosts are not Profesia.
    tracker = FakeTracker([
        offer("O1", "https://jobs.example.com/O1"),
        offer("O2", "https://profesia.sk.evil.com/praca/x/O2"),
        offer("O3"),
    ])
    source = FakeSource({url("O3"): (200, OPEN)})

    result = run(store, tracker, source)

    assert source.fetched == [url("O3")]
    assert result.skipped == 2


def test_fetches_are_paced(store):
    tracker = FakeTracker([offer("O1"), offer("O2"), offer("O3")])
    source = FakeSource({url(o): (200, OPEN) for o in ("O1", "O2", "O3")})
    sleeps: list[float] = []

    run(store, tracker, source, sleeps)

    assert sleeps == [PAUSE_SECONDS, PAUSE_SECONDS]
    assert PAUSE_SECONDS == 2.0


def test_a_fetch_error_is_skipped_not_closed(store):
    tracker = FakeTracker([offer("O1"), offer("O2")])
    source = FakeSource({url("O1"): ConnectionError("reset"), url("O2"): (200, CLOSED)})

    result = run(store, tracker, source)

    assert [item["external_id"] for item in tracker.reports[0]] == ["O2"]
    assert (result.closed, result.skipped) == (1, 1)


def test_a_page_without_a_title_is_skipped(store):
    # Review Focus #1: a captcha answering 200 says nothing about the posting.
    tracker = FakeTracker([offer("O1")])

    result = run(store, tracker, FakeSource({url("O1"): (200, CAPTCHA)}))

    assert tracker.reports == []
    assert result.skipped == 1
    assert store.closure_checked_on() == TODAY


def test_unknown_and_invalid_answers_leave_the_queue_with_a_warning(store, caplog):
    tracker = FakeTracker([offer("O1"), offer("O2")],
                          answer=lambda item: {"O1": "unknown", "O2": "invalid"}[item["external_id"]])
    source = FakeSource({url("O1"): (200, CLOSED), url("O2"): (200, CLOSED)})

    with caplog.at_level(logging.WARNING):
        run(store, tracker, source)

    assert pending(store) == []
    assert "O1: unknown" in caplog.text and "O2: invalid" in caplog.text


def test_a_short_answer_keeps_unmatched_items_pending(store):
    # Review Focus #5: an answer that does not name an item is not an acceptance.
    class ShortTracker(FakeTracker):
        def report_closed(self, closed):
            self.reports.append(closed)
            return [{"external_id": closed[0]["external_id"], "status": "closed"}]

    tracker = ShortTracker([offer("O1"), offer("O2")])
    source = FakeSource({url("O1"): (200, CLOSED), url("O2"): (200, CLOSED)})

    result = run(store, tracker, source)

    assert len(tracker.reports) == 1  # stops instead of resending the same rows
    assert result.settled == 1
    assert len(pending(store)) == 1


def test_profesia_being_down_stops_the_walk_early(store):
    # Found in final review: 50 offers x a 30 s timeout would hold the loop for half an hour.
    offers = [offer(f"O{n}") for n in range(10)]
    source = FakeSource({url(f"O{n}"): ConnectionError("timed out") for n in range(10)})

    result = run(store, FakeTracker(offers), source)

    assert len(source.fetched) == MAX_FAILURES_IN_A_ROW == 3
    assert result.skipped == 10
    assert store.closure_checked_on() == TODAY


def test_a_success_resets_the_failure_count(store):
    pages = {url("O0"): ConnectionError("x"), url("O1"): ConnectionError("x"),
             url("O2"): (200, OPEN), url("O3"): ConnectionError("x"),
             url("O4"): ConnectionError("x"), url("O5"): (200, CLOSED)}
    source = FakeSource(pages)

    result = run(store, FakeTracker([offer(f"O{n}") for n in range(6)]), source)

    assert len(source.fetched) == 6
    assert result.closed == 1


def test_the_walk_is_capped(store, caplog):
    offers = [offer(f"O{n}") for n in range(MAX_FETCHES + 5)]
    source = FakeSource({item["url"]: (200, OPEN) for item in offers})

    with caplog.at_level(logging.WARNING):
        result = run(store, FakeTracker(offers), source)

    assert len(source.fetched) == MAX_FETCHES
    assert result.skipped == 5
    assert "5" in caplog.text and "limit" in caplog.text


def test_a_malformed_offer_is_skipped_not_fatal(store):
    # Found in final review: a crash mid-walk would leave the day unmarked and
    # send Profesia the same requests every fifteen minutes.
    tracker = FakeTracker(["garbage", {"external_id": None, "url": url("O9")}, {"url": 5}, offer("O1")])
    source = FakeSource({url("O1"): (200, CLOSED)})

    result = run(store, tracker, source)

    assert source.fetched == [url("O1")]
    assert (result.closed, result.skipped) == (1, 3)
    assert store.closure_checked_on() == TODAY


def test_the_caller_keeps_the_walk_numbers_when_the_report_fails(store):
    result = CheckResult()
    with pytest.raises(TrackerError):
        check_closures(store, FakeTracker([offer("O1")], fail_post=True),
                       FakeSource({url("O1"): (200, CLOSED)}), TODAY,
                       sleeper=lambda s: None, now=lambda: NOW, result=result)

    assert (result.fetched, result.closed) == (1, 1)


def test_a_big_closed_list_goes_out_in_batches_of_200(store):
    ids = [f"O{n:04d}" for n in range(250)]
    store.finish_closure_check(TODAY, ids, NOW)
    tracker = FakeTracker()

    result = run(store, tracker, FakeSource({}))

    assert [len(batch) for batch in tracker.reports] == [200, 50]
    assert (result.reported, result.settled) == (250, 250)
    assert pending(store) == []
