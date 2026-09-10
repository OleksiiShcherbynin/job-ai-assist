from datetime import date

import pytest

from app.config import ModelQuota, ModelRoles, RunConfig
from app.pacing import Pacer
from app.pipeline import Pipeline
from app.store import Store
from core.models import MatchResult, SearchPreferences, Vacancy, VacancyCard

DAY = date(2026, 9, 10)
EXTRACT, ROUGH, JUDGE = "extract-model", "rough-model", "judge-model"


def card(offer_id, title="Junior Data Engineer", company="ACME", **extra) -> VacancyCard:
    return VacancyCard(
        offer_id=offer_id,
        title=title,
        company=company,
        url=f"https://www.profesia.sk/praca/acme/{offer_id}",
        **extra,
    )


class FakeSource:
    def __init__(self, cards, failing: set[str] | None = None) -> None:
        self._cards = cards
        self._failing = failing or set()
        self.detail_calls: list[str] = []

    def fetch_cards(self):
        return list(self._cards)

    def fetch_detail(self, url):
        self.detail_calls.append(url)
        if any(bad in url for bad in self._failing):
            raise RuntimeError("page unavailable")
        return f"posting text for {url}"


class FakeLLM:
    """Counts calls per stage so a test can assert what the run spent."""

    def __init__(self, rough_scores=None, judge_raises=None) -> None:
        self.rough_scores = rough_scores or {}
        self.judge_raises = judge_raises
        self.extracted: list[str] = []
        self.roughly_scored: list[str] = []
        self.judged: list[str] = []

    def extract_vacancy(self, text):
        self.extracted.append(text)
        return Vacancy(role="Junior Data Engineer", raw_text=text)

    def rough_score(self, vacancy, card):
        self.roughly_scored.append(card.offer_id)
        score = self.rough_scores.get(card.offer_id, 70)
        return MatchResult(score=score, reasons=["rough"], reasons_ru=["грубо"])

    def final_judge(self, vacancy, card):
        if self.judge_raises:
            raise self.judge_raises
        self.judged.append(card.offer_id)
        return MatchResult(score=90, reasons=["judged"], reasons_ru=["разобрано"])


class FrozenClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def build(tmp_path, cards, llm=None, failing=None, **overrides):
    settings = dict(
        min_score=40,
        final_judge_limit=2,
        call_budget=100,
        deal_breakers=["senior"],
    )
    settings.update(overrides)

    config = RunConfig(
        resume_path=tmp_path / "cv.pdf",
        state_path=tmp_path / "seen.db",
        report_dir=tmp_path / "reports",
        list_url="https://example.invalid/",
        start_page=1,
        stop_page=1,
        call_budget=settings["call_budget"],
        min_score=settings["min_score"],
        final_judge_limit=settings["final_judge_limit"],
        preferences=SearchPreferences(desired_roles=[], deal_breakers=settings["deal_breakers"]),
        models=ModelRoles(extract=EXTRACT, rough_score=ROUGH, final_judge=JUDGE),
        quotas={
            EXTRACT: ModelQuota(rpm=100, rpd=500),
            ROUGH: ModelQuota(rpm=100, rpd=500),
            JUDGE: ModelQuota(rpm=100, rpd=settings.get("judge_rpd", 20)),
        },
    )
    store = Store(tmp_path / "seen.db")
    clock = FrozenClock()
    source = FakeSource(cards, failing)
    llm = llm or FakeLLM()
    pipeline = Pipeline(
        config=config,
        source=source,
        store=store,
        pacer=Pacer(store, config, clock=clock.time, sleeper=clock.sleep),
        llm=llm,
    )
    return pipeline, source, store, llm


def test_a_clean_vacancy_travels_the_whole_pipeline(tmp_path):
    pipeline, source, _, llm = build(tmp_path, [card("O1")])

    report = pipeline.run(DAY)

    assert llm.extracted and llm.roughly_scored == ["O1"] and llm.judged == ["O1"]
    assert [item.card.offer_id for item in report.judged] == ["O1"]
    assert report.judged[0].stage == "final"


def test_a_card_rejected_vacancy_never_costs_a_page_load(tmp_path):
    pipeline, source, _, llm = build(tmp_path, [card("O1", title="Senior Architect")])

    report = pipeline.run(DAY)

    assert source.detail_calls == [], "a rejected card must not be fetched"
    assert llm.extracted == []
    assert [item.card.offer_id for item in report.rejected] == ["O1"]


def test_a_vacancy_seen_yesterday_is_not_paid_for_again(tmp_path):
    pipeline, source, store, llm = build(tmp_path, [card("O1")])
    store.record(card("O1"), status="scored")

    report = pipeline.run(DAY)

    assert source.detail_calls == []
    assert llm.roughly_scored == []
    assert report.judged == []


def test_a_repost_inherits_the_original_verdict_without_a_new_call(tmp_path):
    """Softec published the same posting under two offer ids."""
    pipeline, source, store, llm = build(tmp_path, [card("O2", title="Junior IT Analytik", company="Softec")])
    store.record(card("O1", title="Junior IT Analytik", company="Softec"), status="scored", score=77)

    report = pipeline.run(DAY)

    assert llm.roughly_scored == [] and llm.judged == []
    assert [item.score for item in report.judged] == [77]


def test_only_the_best_finalists_reach_the_expensive_judge(tmp_path):
    llm = FakeLLM(rough_scores={"O1": 90, "O2": 80, "O3": 70})
    pipeline, _, _, llm = build(tmp_path, [card("O1"), card("O2"), card("O3")], llm=llm, final_judge_limit=2)

    pipeline.run(DAY)

    assert llm.roughly_scored == ["O1", "O2", "O3"], "everyone gets the cheap score"
    assert llm.judged == ["O1", "O2"], "only the top two reach the judge"


def test_a_vacancy_below_the_threshold_is_never_promoted(tmp_path):
    llm = FakeLLM(rough_scores={"O1": 12})
    pipeline, _, _, llm = build(tmp_path, [card("O1")], llm=llm, min_score=40)

    report = pipeline.run(DAY)

    assert llm.judged == []
    assert report.judged[0].stage == "rough"


def test_an_exhausted_judge_leaves_the_rough_scores_standing(tmp_path):
    """A spent daily quota must degrade the report, not end the run."""
    llm = FakeLLM(rough_scores={"O1": 88}, judge_raises=RuntimeError("429 quota_exceeded"))
    pipeline, _, _, llm = build(tmp_path, [card("O1")], llm=llm)

    report = pipeline.run(DAY)

    assert [item.score for item in report.judged] == [88]
    assert report.judged[0].stage == "rough"
    assert report.failures, "the run must say the judge could not be reached"


def test_one_unreachable_posting_does_not_sink_the_others(tmp_path):
    pipeline, _, _, llm = build(tmp_path, [card("O1"), card("O2")], failing={"O1"})

    report = pipeline.run(DAY)

    assert [item.card.offer_id for item in report.judged] == ["O2"]
    assert [item.title for item in report.failures] != []


def test_the_call_budget_stops_the_run_before_the_quota_does(tmp_path):
    pipeline, _, _, llm = build(tmp_path, [card(f"O{n}") for n in range(1, 6)], call_budget=3)

    report = pipeline.run(DAY)

    spent = len(llm.extracted) + len(llm.roughly_scored) + len(llm.judged)
    assert spent <= 3
    assert report.failures, "vacancies left unprocessed must be named"


def test_the_run_is_recorded_so_today_does_not_repeat(tmp_path):
    pipeline, _, store, _ = build(tmp_path, [card("O1")])

    pipeline.run(DAY)

    assert store.last_run_date() == DAY


def test_what_was_processed_is_remembered_for_tomorrow(tmp_path):
    pipeline, _, store, _ = build(tmp_path, [card("O1")])

    pipeline.run(DAY)

    assert store.is_new("O1") is False
