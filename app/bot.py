"""The Telegram side of the daily run.

Everything the bot does is here, against the Messenger port and the Store, so
it can be tested without a network. Two threads use it, each with its own Bot
and its own Store: the main loop only drains the delivery queue, the listener
only answers commands. The queue therefore has a single sender, and a morning
report cannot go out twice.
"""

import logging
import re
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

from app.report import LANGUAGES
from app.store import Delivery, Store
from core.ports import Messenger

log = logging.getLogger("telegram")

LANGUAGE_KEY = "language"
OFFSET_KEY = "update_offset"

NEED_LANGUAGE = "Сначала выберите язык: /start\nChoose a language first: /start"

TEXTS = {
    "ru": {
        "chosen": "Язык: русский. Отчёт будет приходить сам после ежедневного прогона.",
        "help": "/today — отчёт за сегодня\n/report 14.09 — отчёт за дату\n/start — сменить язык",
        "bad_date": "Не понял дату. Примеры: /report 14.09, /report 14.09.2026, /report 2026-09-14",
        "not_yet": "Сегодняшнего прогона ещё не было.",
        "no_report": "Отчёта за {day} нет.",
        "no_summary": "Выжимки за этот день нет: отчёт появился раньше бота.",
        "file_gone": "Файл отчёта не найден в reports/.",
    },
    "en": {
        "chosen": "Language: English. The report will arrive by itself after the daily run.",
        "help": "/today — today's report\n/report 14.09 — report for a date\n/start — change language",
        "bad_date": "Could not read the date. Examples: /report 14.09, /report 14.09.2026, /report 2026-09-14",
        "not_yet": "Today's run has not happened yet.",
        "no_report": "No report for {day}.",
        "no_summary": "No summary for this day: the report predates the bot.",
        "file_gone": "The report file is missing from reports/.",
    },
}


def parse_report_date(text: str, today: date) -> date | None:
    """A date typed after /report, or None.

    Strict patterns only: the result names a file on disk, so anything that is
    not plainly a date is refused rather than guessed at.
    """
    text = text.strip()
    try:
        if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text):
            year, month, day = map(int, match.groups())
            return date(year, month, day)
        if match := re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text):
            day, month, year = match.groups()
            return date(int(year) if year else today.year, int(month), int(day))
    except ValueError:  # 31.02 and the like
        return None
    return None


class Bot:
    def __init__(
        self,
        messenger: Messenger,
        store: Store,
        report_dir: Path,
        chat_id: int | None,
        wake: threading.Event | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.messenger = messenger
        self.store = store
        self.report_dir = Path(report_dir)
        self.chat_id = chat_id
        self.wake = wake
        self.today = today

    def language(self) -> str | None:
        value = self.store.get_state(LANGUAGE_KEY)
        return value if value in LANGUAGES else None

    def deliver_pending(self) -> None:
        """Sends every queued report, oldest first.

        Stops at the first failure and lets it propagate: that report and the
        ones after it stay queued for the next attempt.
        """
        language = self.language()
        if self.chat_id is None or language is None:
            return
        for delivery in self.store.pending_deliveries():
            self._send(delivery.day, language, delivery)
            self.store.mark_delivered(delivery.day)

    def send_report(self, day: date) -> None:
        """A report on request. Never touches the queue."""
        language = self.language()
        if language is None:
            self._say(NEED_LANGUAGE)
            return

        delivery = self.store.delivery(day)
        if delivery is None and not self._report_path(day, language).is_file():
            texts = TEXTS[language]
            if day == self.today():
                self._say(texts["not_yet"])
            else:
                self._say(texts["no_report"].format(day=day.strftime("%d.%m.%Y")))
            return
        self._send(day, language, delivery)

    def _send(self, day: date, language: str, delivery: Delivery | None) -> None:
        texts = TEXTS[language]
        path = self._report_path(day, language)
        if delivery is not None:
            self._say(delivery.summaries[language])
        if path.is_file():
            # A report from before the bot has no stored summary; say why only the file came.
            self.messenger.send_document(self.chat_id, path, caption=None if delivery else texts["no_summary"])
        else:
            self._say(texts["file_gone"])

    def _report_path(self, day: date, language: str) -> Path:
        # Built from a date object, never from text a user typed.
        return self.report_dir / f"{day.isoformat()}.{language}.md"

    def _say(self, html: str, buttons: list[tuple[str, str]] | None = None, chat_id: int | None = None) -> None:
        self.messenger.send_message(self.chat_id if chat_id is None else chat_id, html, buttons)
