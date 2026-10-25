from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from core.models import VacancyCard


@runtime_checkable
class VacancySource(Protocol):
    """A job board, read in two stages.

    Listing and posting are separate calls on purpose: a card is cheap and
    already carries enough to reject most vacancies, so a posting is only
    fetched once it has survived the card filter.
    """

    def fetch_cards(self) -> list[VacancyCard]:
        """Search-listing rows, deduplicated, without fetching any posting."""
        ...

    def fetch_detail(self, url: str) -> str:
        """The posting's text. Raises ValueError for a host this source
        does not own."""
        ...


@runtime_checkable
class Messenger(Protocol):
    """A chat the bot talks through. Every text is Telegram HTML.

    Updates are passed through as the Bot API's own dicts: the bot reads four
    fields of them, which does not earn a model of its own.
    """

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        """Waits up to `timeout` seconds for updates at or after `offset`."""
        ...

    def send_message(self, chat_id: int, html: str,
                     buttons: list[tuple[str, str]] | None = None) -> None:
        """`buttons` are (label, callback data) pairs shown as one row."""
        ...

    def send_document(self, chat_id: int, path: Path, caption: str | None = None) -> None:
        ...

    def answer_callback(self, callback_id: str) -> None:
        ...

    def edit_message(self, chat_id: int, message_id: int, html: str) -> None:
        ...


@runtime_checkable
class VacancyTracker(Protocol):
    """Where scored vacancies go once judged (job-apply-tracker's ingest API).

    Batches are plain dicts in the tracker's own contract: the shape belongs to
    the other system, and mirroring it in a model here would only drift.
    """

    def push(self, vacancies: list[dict]) -> list[dict]:
        """Sends one batch; returns the tracker's per-item results in order."""
        ...
