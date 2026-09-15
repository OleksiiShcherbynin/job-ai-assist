import logging
import threading
from datetime import date

import pytest

from app.bot import (CHOOSE_LANGUAGE, LANGUAGE_BUTTONS, NEED_LANGUAGE, SETUP, TEXTS, Bot,
                     parse_report_date)
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


# --- commands and buttons --------------------------------------------------

def message(text, chat=CHAT, update_id=1):
    return {"update_id": update_id, "message": {"message_id": 5, "chat": {"id": chat}, "text": text}}


def button(data, chat=CHAT, update_id=2):
    return {"update_id": update_id,
            "callback_query": {"id": "q1", "data": data, "message": {"message_id": 9, "chat": {"id": chat}}}}


def test_start_offers_the_language_buttons(messenger, store, reports):
    make_bot(messenger, store, reports).handle_update(message("/start"))

    assert messenger.sent == [("message", CHAT, CHOOSE_LANGUAGE, LANGUAGE_BUTTONS)]
    assert LANGUAGE_BUTTONS == [("Русский", "lang:ru"), ("English", "lang:en")]


def test_a_command_addressed_by_bot_name_still_works(messenger, store, reports):
    make_bot(messenger, store, reports).handle_update(message("/start@JobAssistBot"))

    assert messenger.sent == [("message", CHAT, CHOOSE_LANGUAGE, LANGUAGE_BUTTONS)]


def test_a_stranger_gets_no_answer_and_changes_nothing(messenger, store, reports):
    bot = make_bot(messenger, store, reports)

    for update in (message("/start", chat=666), message("/today", chat=666), button("lang:ru", chat=666)):
        bot.handle_update(update)

    assert messenger.sent == []
    assert store.get_state("language") is None


def test_without_a_chat_id_start_tells_the_sender_their_own_id(messenger, store, reports):
    make_bot(messenger, store, reports, chat_id=None).handle_update(message("/start", chat=555))

    assert messenger.sent == [("message", 555, SETUP.format(chat_id=555), None)]
    assert "TELEGRAM_CHAT_ID=555" in SETUP.format(chat_id=555)


def test_without_a_chat_id_nothing_else_is_answered(messenger, store, reports):
    bot = make_bot(messenger, store, reports, chat_id=None)

    bot.handle_update(message("/today", chat=555))
    bot.handle_update(button("lang:ru", chat=555))

    assert messenger.sent == []


def test_pressing_a_language_saves_it_confirms_and_wakes_the_loop(messenger, store, reports):
    """Reports may already be queued; they should not wait for the next check."""
    wake = threading.Event()

    make_bot(messenger, store, reports, wake=wake).handle_update(button("lang:en"))

    assert store.get_state("language") == "en"
    assert messenger.sent == [("answer", "q1"), ("edit", CHAT, 9, TEXTS["en"]["chosen"])]
    assert wake.is_set()


def test_an_unknown_button_is_acknowledged_but_changes_nothing(messenger, store, reports):
    make_bot(messenger, store, reports).handle_update(button("lang:sk"))

    assert store.get_state("language") is None
    assert messenger.sent == [("answer", "q1")]


def test_today_sends_todays_report(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(TODAY, SUMMARIES)
    write_report(reports, TODAY, "ru")

    make_bot(messenger, store, reports).handle_update(message("/today"))

    assert messenger.sent[-1] == ("document", CHAT, "2026-09-16.ru.md", None)


def test_report_with_a_date_sends_that_day(messenger, store, reports):
    store.set_state("language", "ru")
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).handle_update(message("/report 15.09"))

    assert messenger.sent == [("document", CHAT, "2026-09-15.ru.md", TEXTS["ru"]["no_summary"])]


@pytest.mark.parametrize("text", ["/report вчера", "/report"])
def test_report_without_a_readable_date_explains_the_formats(messenger, store, reports, text):
    store.set_state("language", "ru")

    make_bot(messenger, store, reports).handle_update(message(text))

    assert messenger.texts() == [TEXTS["ru"]["bad_date"]]


def test_anything_else_gets_the_list_of_commands(messenger, store, reports):
    store.set_state("language", "en")

    make_bot(messenger, store, reports).handle_update(message("hello"))

    assert messenger.texts() == [TEXTS["en"]["help"]]


def test_a_message_without_text_gets_the_list_of_commands(messenger, store, reports):
    update = {"update_id": 1, "message": {"message_id": 5, "chat": {"id": CHAT}, "sticker": {}}}

    make_bot(messenger, store, reports).handle_update(update)

    assert messenger.texts() == [TEXTS["ru"]["help"]]


# --- listening -------------------------------------------------------------

def listen(messenger, store, reports, **bot_options):
    waits: list[float] = []
    make_bot(messenger, store, reports, **bot_options).listen(messenger.stop, wait=waits.append)
    return waits


def test_updates_are_handled_and_the_offset_remembered(store, reports):
    messenger = FakeMessenger(polls=[[message("/start", update_id=10)]])

    listen(messenger, store, reports)

    assert ("message", CHAT, CHOOSE_LANGUAGE, LANGUAGE_BUTTONS) in messenger.sent
    assert store.get_state("update_offset") == "11"
    assert messenger.offsets == [None, 11]


def test_the_offset_survives_a_restart(store, reports):
    """Otherwise a restart re-reads an old /start and answers it again."""
    store.set_state("update_offset", "40")
    messenger = FakeMessenger()

    listen(messenger, store, reports)

    assert messenger.offsets == [40]


def test_a_rejected_token_stops_the_listener_with_one_clear_line(store, reports, caplog):
    messenger = FakeMessenger(polls=[TelegramError("getUpdates: 401 Unauthorized", status=401),
                                     [message("/start")]])

    listen(messenger, store, reports)

    assert messenger.sent == []
    assert len(messenger.polls) == 1, "nothing is polled after a 401"
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_an_outage_backs_off_doubling_up_to_five_minutes(store, reports):
    messenger = FakeMessenger(polls=[TelegramError("down")] * 9)

    waits = listen(messenger, store, reports)

    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 300]


def test_telegrams_retry_after_is_respected(store, reports):
    messenger = FakeMessenger(polls=[TelegramError("slow down", status=429, retry_after=30)])

    assert listen(messenger, store, reports) == [30]


def test_an_outage_is_logged_once_and_its_end_once(store, reports, caplog):
    """Nobody watches this log live; a line per retry would bury everything else."""
    caplog.set_level(logging.INFO, logger="telegram")
    messenger = FakeMessenger(polls=[TelegramError("down")] * 3 + [[]])

    listen(messenger, store, reports)

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "reachable again" in caplog.text


def test_an_update_that_breaks_the_handler_is_skipped_not_retried(store, reports):
    store.set_state("language", "ru")
    messenger = FakeMessenger(polls=[[message("/today", update_id=3)]])
    messenger.fail_on.add("message")

    listen(messenger, store, reports)

    assert store.get_state("update_offset") == "4"
    assert messenger.offsets == [None, 4]
