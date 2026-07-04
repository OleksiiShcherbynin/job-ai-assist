from __future__ import annotations
from datetime import datetime
from typing import Protocol

from core.models import Attachment, IncomingMessage

class VacancySource(Protocol):

    def fetch_vacancies(self, limit: int = 15) -> list[dict]: ... 

class MessageSender(Protocol):

    def send(self, to: str, subject: list[Attachment] | None = None) -> None: ...

class MessageReceiver(Protocol):

    def fetch_unread(self, limit: int = 5) -> list[IncomingMessage]: ...
    