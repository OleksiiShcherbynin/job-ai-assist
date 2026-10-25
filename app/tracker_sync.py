"""Sends the tracker outbox to job-apply-tracker, batch by batch.

Payloads are built from the vacancies table at send time, so whatever was
re-scored since queueing goes out with its latest numbers.
"""

import json
import logging
from dataclasses import dataclass

from app.store import Store
from core.ports import VacancyTracker

log = logging.getLogger("daily-run")

BATCH_SIZE = 200
"""The tracker's per-request limit."""

ACCEPTED = ("created", "updated")


@dataclass
class SyncResult:
    sent: int = 0
    accepted: int = 0
    invalid: int = 0


def _lines(reasons: list[str]) -> list[str]:
    return [reason.strip() for reason in reasons if reason and reason.strip()]


def to_payload(row) -> dict:
    """One item of the tracker's ingest contract from a tracker_pending row."""
    card = json.loads(row["card_json"] or "{}")
    reasons = json.loads(row["reasons_json"] or "{}")
    # A repost's own status says only "repost-of-…"; its stage is the original's.
    stage_status = row["original_status"] or row["status"]
    return {
        "external_id": row["offer_id"],
        "title": row["title"],
        "company": row["company"] or "",
        "url": row["url"],
        "location": card.get("location") or "",
        "salary": card.get("salary_text") or "",
        "score": row["score"],
        "score_stage": "final" if stage_status == "scored-final" else "rough",
        "reasons_en": _lines(reasons.get("en", [])),
        "reasons_ru": _lines(reasons.get("ru", [])),
        "first_seen_at": row["first_seen"],
    }


def sync(store: Store, tracker: VacancyTracker, batch_size: int = BATCH_SIZE) -> SyncResult:
    """Drains the outbox. A TrackerError propagates and leaves the batch queued."""
    result = SyncResult()
    while True:
        rows = store.tracker_pending(batch_size)
        if not rows:
            return result

        batch = [to_payload(row) for row in rows]
        answers = tracker.push(batch)
        result.sent += len(batch)

        settled = []
        for item, answer in zip(batch, answers, strict=False):
            if answer.get("external_id") != item["external_id"]:
                continue  # out of step with what we sent: keep it queued
            if answer.get("status") in ACCEPTED:
                result.accepted += 1
                settled.append(item["external_id"])
            elif answer.get("status") == "invalid":
                # Retrying cannot fix bad data; say so once and let it go.
                log.warning("the tracker refused %s: %s", item["external_id"], answer.get("errors"))
                result.invalid += 1
                settled.append(item["external_id"])

        store.tracker_done(settled)
        if len(settled) < len(batch):
            # Something stayed queued; the next check retries it rather than
            # this loop resending the same rows forever.
            return result
