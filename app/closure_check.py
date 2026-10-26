"""Once a day: which of the tracker's offers has Profesia closed?

The tracker knows which offers matter (applications waiting for an answer,
inbox vacancies), this side already talks to Profesia, so the tracker hands
out the list and gets back the closed ones.

The walk happens at most once a day. Its findings are stored before anything
is sent, so a tracker that refuses the report gets it again on a later check
from the store, and Profesia is never asked twice for the same day.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable
from urllib.parse import urlparse

from app.store import Store
from core.closure import posting_state
from core.ports import VacancySource, VacancyTracker

log = logging.getLogger("daily-run")

PAUSE_SECONDS = 2.0
"""Between posting fetches: tens of offers a day, so a gentle pace costs a minute."""

BATCH_SIZE = 200
"""The tracker's per-request limit."""

MAX_FETCHES = 100
"""A day's ceiling. The list is expected to hold tens of offers; the inbox only
grows until postings close, and nothing else stops it from reaching hundreds."""

MAX_FAILURES_IN_A_ROW = 3
"""Profesia unreachable: with a 30 s timeout per page, going on would hold the
daily loop for half an hour to learn nothing."""

PROFESIA_HOSTS = ("www.profesia.sk", "profesia.sk")

# Every answer the tracker can give settles an item: closed/unchanged are done,
# unknown/invalid cannot be fixed by sending the same thing again.
_SETTLED = ("closed", "unchanged", "unknown", "invalid")
_NOT_APPLIED = ("unknown", "invalid")


@dataclass
class CheckResult:
    listed: int = 0
    fetched: int = 0
    closed: int = 0
    skipped: int = 0
    reported: int = 0
    settled: int = 0


def _is_profesia(url: str) -> bool:
    return (urlparse(url).hostname or "") in PROFESIA_HOSTS


def report_pending(store: Store, tracker: VacancyTracker, result: CheckResult) -> None:
    """Sends the stored closed offers. A TrackerError propagates and keeps them."""
    while True:
        rows = store.closed_pending(BATCH_SIZE)
        if not rows:
            return

        batch = [{"external_id": row["offer_id"], "seen_at": row["seen_at"]} for row in rows]
        answers = tracker.report_closed(batch)
        result.reported += len(batch)

        settled = []
        for item, answer in zip(batch, answers, strict=False):
            if answer.get("external_id") != item["external_id"]:
                continue  # out of step with what we sent: keep it pending
            status = answer.get("status")
            if status in _NOT_APPLIED:
                log.warning("the tracker did not apply the closure of %s: %s",
                            item["external_id"], status)
            if status in _SETTLED:
                settled.append(item["external_id"])

        store.closed_done(settled)
        result.settled += len(settled)
        if len(settled) < len(batch):
            # Retried on the next check rather than resending the same rows here.
            return


def walk(tracker_offers: list[dict], source: VacancySource,
         sleeper: Callable[[float], None], result: CheckResult) -> list[str]:
    """Fetches each Profesia posting; returns the offer ids found closed.

    Never raises: anything that goes wrong with one offer skips that offer, and
    a crash here would leave the day unmarked and repeat the walk every check.
    """
    closed: list[str] = []
    failures_in_a_row = 0
    for index, item in enumerate(tracker_offers):
        if failures_in_a_row >= MAX_FAILURES_IN_A_ROW or result.fetched >= MAX_FETCHES:
            left = len(tracker_offers) - index
            result.skipped += left
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                log.warning("Profesia is not answering; %d postings left for tomorrow", left)
            else:
                log.warning("reached the limit of %d postings a day; %d left for tomorrow",
                            MAX_FETCHES, left)
            break

        offer_id = item.get("external_id") if isinstance(item, dict) else None
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(offer_id, str) or not offer_id or not isinstance(url, str) \
                or not _is_profesia(url):
            result.skipped += 1
            continue

        if result.fetched:
            sleeper(PAUSE_SECONDS)
        result.fetched += 1
        try:
            status, html = source.fetch_page(url)
            state = posting_state(status, html)
        except Exception as error:
            # Not knowing is not "closed": the offer is simply checked tomorrow.
            log.info("could not load %s, checking it again tomorrow (%s)", offer_id, error)
            result.skipped += 1
            failures_in_a_row += 1
            continue

        failures_in_a_row = 0
        if state == "closed":
            closed.append(offer_id)
        elif state == "unknown":
            result.skipped += 1
    result.closed = len(closed)
    return closed


def check_closures(
    store: Store,
    tracker: VacancyTracker,
    source: VacancySource,
    today: date,
    sleeper: Callable[[float], None] | None = None,
    now: Callable[[], datetime] | None = None,
    result: CheckResult | None = None,
) -> CheckResult:
    """One check. TrackerError propagates; what was found is stored first.

    Pass `result` to keep the walk's numbers when the report afterwards fails.
    """
    result = result if result is not None else CheckResult()
    report_pending(store, tracker, result)
    if store.closure_checked_on() == today:
        return result

    # Failing here leaves the day unmarked, so the next check asks again.
    offers = tracker.offers_to_check()
    result.listed = len(offers)
    closed = walk(offers, source, sleeper or time.sleep, result)

    seen_at = (now or (lambda: datetime.now(timezone.utc)))()
    store.finish_closure_check(today, closed, seen_at)
    report_pending(store, tracker, result)
    return result
