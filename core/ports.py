from __future__ import annotations

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
