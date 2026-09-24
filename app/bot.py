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
from local_connectors.telegram import TelegramError

log = logging.getLogger("telegram")

LANGUAGE_KEY = "language"
OFFSET_KEY = "update_offset"

POLL_SECONDS = 50
"""How long Telegram may hold a getUpdates request open. Long polling answers
a button within a second while costing one idle request a minute."""

FIRST_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300

NEED_LANGUAGE = "Сначала выберите язык: /start\nChoose a language first: /start"

CHOOSE_LANGUAGE = "Язык отчёта / Report language"
LANGUAGE_BUTTONS = [("Русский", "lang:ru"), ("English", "lang:en")]

SETUP = (
    "Ваш chat id: <code>{chat_id}</code>\n"
    "Добавьте в .env строку TELEGRAM_CHAT_ID={chat_id} и пересоздайте контейнер: docker compose up -d\n\n"
    "Your chat id: <code>{chat_id}</code>\n"
    "Add TELEGRAM_CHAT_ID={chat_id} to .env and recreate the container: docker compose up -d"
)

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

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_button(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def listen(self, stop: threading.Event, wait: Callable[[float], object] | None = None) -> None:
        """Answers commands until `stop` is set or the token is rejected."""
        wait = wait or stop.wait
        delay = 0
        while not stop.is_set():
            stored = self.store.get_state(OFFSET_KEY)
            try:
                updates = self.messenger.get_updates(int(stored) if stored else None, POLL_SECONDS)
            except TelegramError as error:
                if error.status == 401:
                    log.error("Telegram rejected TELEGRAM_BOT_TOKEN — check .env. The bot stays off "
                              "until the container is recreated; the daily run is not affected.")
                    return
                if delay == 0:
                    log.warning("Telegram is unreachable; retrying quietly until it answers (%s)", error)
                delay = min(max(delay * 2, FIRST_BACKOFF_SECONDS), MAX_BACKOFF_SECONDS)
                wait(max(delay, error.retry_after or 0))
                continue

            if delay:
                log.info("Telegram is reachable again")
                delay = 0
            for update in updates:
                # Saved before handling: an update that keeps breaking the
                # handler must not be fetched again forever.
                self.store.set_state(OFFSET_KEY, str(update["update_id"] + 1))
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("could not answer a Telegram update")

    def _on_message(self, message: dict) -> None:
        sender = (message.get("chat") or {}).get("id")
        command, _, argument = (message.get("text") or "").strip().partition(" ")
        # From a group or the command menu, commands arrive as /start@BotName.
        command = command.split("@", 1)[0]

        if self.chat_id is None:
            # Setup mode. The sender's own id is the only thing worth saying,
            # and it tells a stranger nothing they did not already have.
            if command == "/start" and sender is not None:
                self._say(SETUP.format(chat_id=sender), chat_id=sender)
            return
        if sender != self.chat_id:
            return

        texts = TEXTS[self.language() or "ru"]
        if command == "/start":
            self._say(CHOOSE_LANGUAGE, buttons=LANGUAGE_BUTTONS)
        elif command == "/today":
            self.send_report(self.today())
        elif command == "/report":
            day = parse_report_date(argument, self.today())
            if day is None:
                self._say(texts["bad_date"])
            else:
                self.send_report(day)
        else:
            self._say(texts["help"])

    def _on_button(self, query: dict) -> None:
        message = query.get("message") or {}
        if self.chat_id is None or (message.get("chat") or {}).get("id") != self.chat_id:
            return

        # Acknowledged first, whatever it was: otherwise the button keeps spinning.
        self.messenger.answer_callback(query["id"])
        data = query.get("data") or ""
        language = data.removeprefix("lang:")
        if not data.startswith("lang:") or language not in LANGUAGES:
            return

        self.store.set_state(LANGUAGE_KEY, language)
        self.messenger.edit_message(self.chat_id, message["message_id"], TEXTS[language]["chosen"])
        if self.wake is not None:
            # Reports may have been queued waiting for exactly this.
            self.wake.set()

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
