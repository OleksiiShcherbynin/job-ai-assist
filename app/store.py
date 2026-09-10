"""What the daily run remembers between invocations.

One SQLite file holds everything: which vacancies have been seen, how many API
calls each model has spent today, and when the last run happened. SQLite rather
than JSON because a JSON store rewrites the whole file per write and loses
everything if the container is killed mid-write.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.logic import strip_accents
from core.models import VacancyCard

# Google resets the daily quota at midnight Pacific, so the quota day and the
# local calendar day do not line up: an 08:00 run in Bratislava is still
# spending the previous quota day.
_QUOTA_TZ = ZoneInfo("America/Los_Angeles")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    offer_id     TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    title        TEXT NOT NULL,
    company      TEXT,
    url          TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT,
    card_json    TEXT,
    detail_text  TEXT,
    vacancy_json TEXT,
    score        INTEGER,
    reasons_json TEXT
);
CREATE INDEX IF NOT EXISTS vacancies_fingerprint ON vacancies (fingerprint);

CREATE TABLE IF NOT EXISTS api_calls (
    quota_day TEXT NOT NULL,
    model     TEXT NOT NULL,
    calls     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (quota_day, model)
);

CREATE TABLE IF NOT EXISTS runs (
    run_date  TEXT PRIMARY KEY,
    finished  TEXT NOT NULL
);
"""


def quota_day(moment: datetime) -> date:
    """The Google quota day a moment belongs to."""
    return moment.astimezone(_QUOTA_TZ).date()


@dataclass
class Verdict:
    """What a previous run decided about a vacancy."""

    status: str
    score: int | None
    reasons: list[str] = field(default_factory=list)
    reasons_ru: list[str] = field(default_factory=list)


def fingerprint(card: VacancyCard) -> str:
    """Identity of the posting itself, independent of its offer id.

    Employers repost: Softec published one vacancy as O5321098 and again as
    O5350242 with a byte-identical title. Offer-id dedup alone misses that.
    """
    title = " ".join(strip_accents((card.title or "").lower()).split())
    company = " ".join(strip_accents((card.company or "").lower()).split())
    return f"{company}|{title}"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def is_new(self, offer_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM vacancies WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        return row is None

    def record(
        self,
        card: VacancyCard,
        status: str,
        reason: str | None = None,
        detail_text: str | None = None,
        vacancy_json: str | None = None,
        score: int | None = None,
        reasons: list[str] | None = None,
        reasons_ru: list[str] | None = None,
        moment: datetime | None = None,
    ) -> None:
        now = (moment or datetime.now(timezone.utc)).isoformat()
        # None means "this write says nothing about the reasons", which the
        # upsert below preserves. An empty list means "there are none".
        reasons_json = (
            json.dumps({"en": reasons or [], "ru": reasons_ru or []}, ensure_ascii=False)
            if reasons is not None or reasons_ru is not None
            else None
        )
        self._connection.execute(
            """
            INSERT INTO vacancies (offer_id, fingerprint, title, company, url,
                                   first_seen, last_seen, status, reason,
                                   card_json, detail_text, vacancy_json, score, reasons_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_id) DO UPDATE SET
                last_seen    = excluded.last_seen,
                status       = excluded.status,
                reason       = excluded.reason,
                card_json    = excluded.card_json,
                -- The final judgement arrives as a second write and must replace
                -- the rough verdict; leaving it out kept 98 where the judge said
                -- 78, and a repost would have inherited the wrong number.
                score        = COALESCE(excluded.score, vacancies.score),
                reasons_json = COALESCE(excluded.reasons_json, vacancies.reasons_json),
                -- That write carries no posting text, and re-scoring later
                -- without re-scraping depends on the stored copy surviving.
                detail_text  = COALESCE(excluded.detail_text, vacancies.detail_text),
                vacancy_json = COALESCE(excluded.vacancy_json, vacancies.vacancy_json)
            """,
            (
                card.offer_id,
                fingerprint(card),
                card.title,
                card.company,
                card.url,
                now,
                now,
                status,
                reason,
                card.model_dump_json(),
                detail_text,
                vacancy_json,
                score,
                reasons_json,
            ),
        )
        self._connection.commit()

    def verdict(self, offer_id: str) -> "Verdict | None":
        row = self._connection.execute(
            "SELECT status, score, reasons_json FROM vacancies WHERE offer_id = ?",
            (offer_id,),
        ).fetchone()
        if row is None:
            return None

        reasons = json.loads(row["reasons_json"] or "{}")
        return Verdict(
            status=row["status"],
            score=row["score"],
            reasons=reasons.get("en", []),
            reasons_ru=reasons.get("ru", []),
        )

    def find_repost(self, card: VacancyCard) -> str | None:
        """The offer id this posting was already seen under, if any."""
        row = self._connection.execute(
            "SELECT offer_id FROM vacancies WHERE fingerprint = ? AND offer_id != ? "
            "ORDER BY first_seen LIMIT 1",
            (fingerprint(card), card.offer_id),
        ).fetchone()
        return row["offer_id"] if row else None

    def calls_used(self, model: str, moment: datetime | None = None) -> int:
        day = quota_day(moment or datetime.now(timezone.utc)).isoformat()
        row = self._connection.execute(
            "SELECT calls FROM api_calls WHERE quota_day = ? AND model = ?", (day, model)
        ).fetchone()
        return row["calls"] if row else 0

    def record_call(self, model: str, moment: datetime | None = None) -> None:
        day = quota_day(moment or datetime.now(timezone.utc)).isoformat()
        self._connection.execute(
            "INSERT INTO api_calls (quota_day, model, calls) VALUES (?, ?, 1) "
            "ON CONFLICT(quota_day, model) DO UPDATE SET calls = calls + 1",
            (day, model),
        )
        self._connection.commit()

    def last_run_date(self) -> date | None:
        row = self._connection.execute(
            "SELECT run_date FROM runs ORDER BY run_date DESC LIMIT 1"
        ).fetchone()
        return date.fromisoformat(row["run_date"]) if row else None

    def mark_run(self, day: date) -> None:
        self._connection.execute(
            "INSERT INTO runs (run_date, finished) VALUES (?, ?) "
            "ON CONFLICT(run_date) DO UPDATE SET finished = excluded.finished",
            (day.isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        self._connection.commit()
