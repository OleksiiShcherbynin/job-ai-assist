import threading
from datetime import date

import pytest

from app.bot import NEED_LANGUAGE, TEXTS, Bot, parse_report_date
from app.store import Store
from local_connectors.telegram import TelegramError

CHAT = 1001
TODAY = date(2026, 9, 16)
DAY = date(2026, 9, 15)
SUMMARIES = {"en": "<b>Vacancies — 2026-09-15</b>", "ru": "<b>Вакансии — 2026-09-15</b>"}


class FakeMessenger:
    """Records what the bot sends. `polls` scripts get_updates: each item is a
    list of updates or an exception to raise; once exhausted it sets `stop`."""

    def __init__(self, polls=()):
        self.sent: list[tuple] = []
        self.fail_on: set[str] = set()
        self.polls = list(polls)
        self.offsets: list[int | None] = []
        self.stop = threading.Event()

    def _record(self, kind, *details):
        if kind in self.fail_on:
            raise TelegramError(f"{kind} failed", status=500)
        self.sent.append((kind, *details))

    def get_updates(self, offset, timeout):
        self.offsets.append(offset)
        if not self.polls:
            self.stop.set()
            return []
        outcome = self.polls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def send_message(self, chat_id, html, buttons=None):
        self._record("message", chat_id, html, buttons)

    def send_document(self, chat_id, path, caption=None):
        self._record("document", chat_id, path.name, caption)

    def answer_callback(self, callback_id):
        self._record("answer", callback_id)

    def edit_message(self, chat_id, message_id, html):
        self._record("edit", chat_id, message_id, html)

    def texts(self):
        return [item[2] for item in self.sent if item[0] == "message"]


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "seen.db")
    yield opened
    opened.close()


@pytest.fixture
def reports(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    return folder


@pytest.fixture
def messenger():
    return FakeMessenger()


def make_bot(messenger, store, reports, chat_id=CHAT, wake=None) -> Bot:
    return Bot(messenger, store, reports, chat_id, wake=wake, today=lambda: TODAY)


def write_report(reports, day, language):
    path = reports / f"{day.isoformat()}.{language}.md"
    path.write_text("# report", encoding="utf-8")
    return path


# --- dates -----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2026-09-14", date(2026, 9, 14)),
    ("14.09", date(2026, 9, 14)),
    ("14.09.2026", date(2026, 9, 14)),
    ("4.9.2025", date(2025, 9, 4)),
    ("  14.09  ", date(2026, 9, 14)),
])
def test_report_dates_are_read_in_the_accepted_formats(text, expected):
    assert parse_report_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["", "yesterday", "31.02", "2026-9-14", "../../etc/passwd", "14/09"])
def test_anything_else_is_not_a_date(text):
    assert parse_report_date(text, TODAY) is None


# --- the queue -------------------------------------------------------------

def test_a_queued_report_goes_out_as_summary_then_file(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.sent == [
        ("message", CHAT, SUMMARIES["ru"], None),
        ("document", CHAT, "2026-09-15.ru.md", None),
    ]
    assert store.pending_deliveries() == []


def test_nothing_is_sent_before_a_language_is_chosen(messenger, store, reports):
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.sent == []
    assert len(store.pending_deliveries()) == 1


def test_nothing_is_sent_without_a_chat_id(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)

    make_bot(messenger, store, reports, chat_id=None).deliver_pending()

    assert messenger.sent == []
    assert len(store.pending_deliveries()) == 1


def test_a_failed_send_keeps_the_report_queued(messenger, store, reports):
    store.set_state("language", "en")
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "en")
    messenger.fail_on.add("document")

    with pytest.raises(TelegramError):
        make_bot(messenger, store, reports).deliver_pending()

    assert len(store.pending_deliveries()) == 1


def test_queued_reports_go_out_oldest_first(messenger, store, reports):
    store.set_state("language", "en")
    store.queue_delivery(date(2026, 9, 15), {"en": "fifteenth", "ru": "-"})
    store.queue_delivery(date(2026, 9, 14), {"en": "fourteenth", "ru": "-"})

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.texts()[0] == "fourteenth"
    assert messenger.texts().index("fifteenth") > 0


def test_a_missing_file_still_sends_the_summary_and_says_so(messenger, store, reports):
    store.set_state("language", "en")
    store.queue_delivery(DAY, SUMMARIES)

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.texts() == [SUMMARIES["en"], TEXTS["en"]["file_gone"]]
    assert store.pending_deliveries() == []


# --- on demand -------------------------------------------------------------

def test_a_requested_day_gets_its_summary_and_file(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)
    store.mark_delivered(DAY)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).send_report(DAY)

    assert messenger.sent == [
        ("message", CHAT, SUMMARIES["ru"], None),
        ("document", CHAT, "2026-09-15.ru.md", None),
    ]


def test_on_demand_sending_leaves_the_queue_alone(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).send_report(DAY)

    assert len(store.pending_deliveries()) == 1


def test_a_day_from_before_the_bot_gets_the_file_with_a_note(messenger, store, reports):
    store.set_state("language", "ru")
    write_report(reports, date(2026, 9, 14), "ru")

    make_bot(messenger, store, reports).send_report(date(2026, 9, 14))

    assert messenger.sent == [("document", CHAT, "2026-09-14.ru.md", TEXTS["ru"]["no_summary"])]


def test_a_day_without_a_report_says_so(messenger, store, reports):
    store.set_state("language", "ru")

    make_bot(messenger, store, reports).send_report(date(2026, 9, 1))

    assert messenger.texts() == ["Отчёта за 01.09.2026 нет."]


def test_today_before_the_run_says_the_run_has_not_happened(messenger, store, reports):
    store.set_state("language", "en")

    make_bot(messenger, store, reports).send_report(TODAY)

    assert messenger.texts() == [TEXTS["en"]["not_yet"]]


def test_a_request_before_choosing_a_language_asks_for_one(messenger, store, reports):
    make_bot(messenger, store, reports).send_report(DAY)

    assert messenger.texts() == [NEED_LANGUAGE]
