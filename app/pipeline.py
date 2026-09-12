"""One morning's work, in order.

The shape follows the quotas rather than convenience: every step that costs
something is guarded by a cheaper step that can rule the vacancy out first.
Cards are filtered before a page is fetched, postings are filtered before the
judge is called, and the judge — twenty calls a day — only ever sees finalists.

Nothing here fails the whole run. A dead page, a spent quota or an exhausted
budget removes one vacancy and is named in the report; the remaining ones carry
on.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.config import RunConfig
from app.pacing import AccountBlocked, DailyQuotaExhausted, Pacer, RateLimitKind, classify_rate_limit
from app.report import Failure, Judged, Rejected, RunReport
from app.store import Store
from core.logic import card_rejection_reason, rejection_reason
from core.models import MatchResult, Vacancy, VacancyCard
from core.ports import VacancySource

log = logging.getLogger(__name__)


class VacancyJudge(Protocol):
    """The three model-backed steps, kept behind one seam so tests never call a
    model and the pipeline never imports one."""

    def extract_vacancy(self, text: str) -> Vacancy: ...
    def rough_score(self, vacancy: Vacancy, card: VacancyCard) -> MatchResult: ...
    def final_judge(self, vacancy: Vacancy, card: VacancyCard) -> MatchResult: ...


@dataclass
class _Budget:
    """A stop-loss across all models, independent of any single quota."""

    limit: int
    spent: int = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def spend(self) -> None:
        self.spent += 1


@dataclass
class _Candidate:
    card: VacancyCard
    vacancy: Vacancy
    result: MatchResult


class Pipeline:
    def __init__(
        self,
        config: RunConfig,
        source: VacancySource,
        store: Store,
        pacer: Pacer,
        llm: VacancyJudge,
        resume_text: str = "",
    ) -> None:
        self.config = config
        self.source = source
        self.store = store
        self.pacer = pacer
        self.llm = llm
        self.resume_text = resume_text

    def run(self, day: date) -> RunReport:
        report = RunReport(day=day, seen=0)
        budget = _Budget(self.config.call_budget)

        cards = self.source.fetch_cards()
        report.seen = len(cards)

        fresh = [card for card in cards if self.store.is_new(card.offer_id)]
        survivors = self._drop_by_card(fresh, report)
        survivors = self._settle_reposts(survivors, report)

        try:
            candidates = self._describe_and_score(survivors, report, budget)
            self._judge_finalists(candidates, report, budget)
        except AccountBlocked as blocked:
            # Nothing will answer and nothing will change by waiting, so stop
            # rather than repeating the refusal for every remaining vacancy.
            log.error("stopping the run: %s", blocked)
            report.failures.append(Failure(
                title="whole run", url="https://ai.studio/projects", error=str(blocked),
            ))
            return report

        self.store.mark_run(day)
        return report

    def _drop_by_card(self, cards: list[VacancyCard], report: RunReport) -> list[VacancyCard]:
        kept = []
        for card in cards:
            reason = card_rejection_reason(card, self.config.preferences)
            if reason:
                report.rejected.append(Rejected(card=card, reason=reason))
                self.store.record(card, status="rejected-card", reason=reason)
            else:
                kept.append(card)
        return kept

    def _settle_reposts(self, cards: list[VacancyCard], report: RunReport) -> list[VacancyCard]:
        """A posting already judged under another offer id keeps its verdict."""
        kept = []
        for card in cards:
            original = self.store.find_repost(card)
            verdict = self.store.verdict(original) if original else None
            if verdict is None or verdict.score is None:
                kept.append(card)
                continue

            stage = "final" if verdict.status == "scored-final" else "rough"
            report.judged.append(Judged(
                card=card,
                score=verdict.score,
                stage=stage,
                reasons=verdict.reasons,
                reasons_ru=verdict.reasons_ru,
            ))
            self.store.record(
                card, status=f"repost-of-{original}", score=verdict.score,
                reasons=verdict.reasons, reasons_ru=verdict.reasons_ru,
            )
        return kept

    def _describe_and_score(
        self, cards: list[VacancyCard], report: RunReport, budget: _Budget
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []

        for card in cards:
            if budget.exhausted:
                self._give_up(card, report, "call budget spent before this vacancy")
                continue

            try:
                text = self.source.fetch_detail(card.url)
            except Exception as error:
                self._give_up(card, report, f"posting unreachable: {error}")
                continue

            vacancy = self._spend(self.config.models.extract, budget, self.llm.extract_vacancy, text)
            if isinstance(vacancy, Exception):
                self._give_up(card, report, f"could not read the posting: {vacancy}")
                continue
            vacancy.raw_text = text

            reason = rejection_reason(vacancy, self.config.preferences)
            if reason:
                report.rejected.append(Rejected(card=card, reason=reason))
                self.store.record(card, status="rejected-detail", reason=reason, detail_text=text)
                continue

            if budget.exhausted:
                self._give_up(card, report, "call budget spent before scoring")
                continue

            result = self._spend(
                self.config.models.rough_score, budget, self.llm.rough_score, vacancy, card
            )
            if isinstance(result, Exception):
                self._give_up(card, report, f"could not be scored: {result}")
                continue

            candidates.append(_Candidate(card=card, vacancy=vacancy, result=result))
            report.judged.append(Judged(
                card=card, score=result.score, stage="rough",
                reasons=result.reasons, reasons_ru=result.reasons_ru,
            ))
            self.store.record(
                card, status="scored-rough", score=result.score, detail_text=text,
                reasons=result.reasons, reasons_ru=result.reasons_ru,
            )

        return candidates

    def _judge_finalists(
        self, candidates: list[_Candidate], report: RunReport, budget: _Budget
    ) -> None:
        finalists = sorted(
            (c for c in candidates if c.result.score >= self.config.min_score),
            key=lambda c: c.result.score,
            reverse=True,
        )[: self.config.final_judge_limit]

        for candidate in finalists:
            if budget.exhausted:
                report.failures.append(Failure(
                    candidate.card.title, candidate.card.url,
                    "call budget spent before the final judgement",
                ))
                continue

            verdict = self._spend(
                self.config.models.final_judge, budget,
                self.llm.final_judge, candidate.vacancy, candidate.card,
            )
            if isinstance(verdict, Exception):
                # The rough score already stands in the report; say why it was
                # never upgraded rather than dropping the vacancy.
                report.failures.append(Failure(
                    candidate.card.title, candidate.card.url,
                    f"not judged in full: {verdict}",
                ))
                continue

            self._replace_verdict(report, candidate.card, verdict)
            self.store.record(
                candidate.card, status="scored-final", score=verdict.score,
                reasons=verdict.reasons, reasons_ru=verdict.reasons_ru,
            )

    def _spend(self, model: str, budget: _Budget, call, *args):
        """Pace, call, account. Returns the result or the exception to report."""
        try:
            self.pacer.wait_for_slot(model)
        except DailyQuotaExhausted as exhausted:
            return exhausted

        try:
            return call(*args)
        except Exception as error:
            if classify_rate_limit(error) is RateLimitKind.ACCOUNT:
                raise AccountBlocked(f"the API refused everything: {error}") from error
            log.warning("%s failed: %s", model, error)
            return error
        finally:
            # A refused request still counted against the quota, so account for
            # it whether the call returned, failed, or aborted the run.
            self.pacer.record(model)
            budget.spend()

    def _replace_verdict(self, report: RunReport, card: VacancyCard, verdict: MatchResult) -> None:
        for index, item in enumerate(report.judged):
            if item.card.offer_id == card.offer_id:
                report.judged[index] = Judged(
                    card=card, score=verdict.score, stage="final",
                    reasons=verdict.reasons, reasons_ru=verdict.reasons_ru,
                )
                return

    def _give_up(self, card: VacancyCard, report: RunReport, why: str) -> None:
        report.failures.append(Failure(card.title, card.url, why))
        self.store.record(card, status="failed", reason=why)
