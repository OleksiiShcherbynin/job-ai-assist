from datetime import date

import pytest

from app.config import ModelQuota, RunConfig
from app.main import already_ran_today, write_reports
from app.report import Judged, RunReport
from app.store import Store
from core.models import SearchPreferences, VacancyCard

DAY = date(2026, 9, 10)


def config_at(tmp_path) -> RunConfig:
    return RunConfig(
        resume_path=tmp_path / "cv.pdf",
        state_path=tmp_path / "seen.db",
        report_dir=tmp_path / "reports",
        list_url="https://example.invalid/",
        start_page=1,
        stop_page=1,
        call_budget=10,
        min_score=40,
        final_judge_limit=2,
        preferences=SearchPreferences(desired_roles=[]),
        quotas={"m": ModelQuota(rpm=1, rpd=1)},
    )


@pytest.fixture
def report() -> RunReport:
    card = VacancyCard(offer_id="O1", title="Junior Data Engineer",
                       url="https://www.profesia.sk/praca/acme/O1", company="ACME")
    return RunReport(day=DAY, seen=99, judged=[
        Judged(card=card, score=87, stage="final",
               reasons=["stack matches"], reasons_ru=["стек совпадает"]),
    ])


def test_both_language_files_are_written(tmp_path, report):
    written = write_reports(report, config_at(tmp_path))

    assert {path.name for path in written} == {"2026-09-10.en.md", "2026-09-10.ru.md"}
    assert all(path.exists() for path in written)


def test_each_file_holds_its_own_language(tmp_path, report):
    written = write_reports(report, config_at(tmp_path))
    by_name = {path.name: path.read_text(encoding="utf-8") for path in written}

    assert "stack matches" in by_name["2026-09-10.en.md"]
    assert "стек совпадает" in by_name["2026-09-10.ru.md"]
    assert "стек совпадает" not in by_name["2026-09-10.en.md"]


def test_the_report_directory_is_created_if_missing(tmp_path, report):
    config = config_at(tmp_path)
    assert not config.report_dir.exists()

    write_reports(report, config)

    assert config.report_dir.is_dir()


def test_rerunning_the_same_day_overwrites_rather_than_piling_up(tmp_path, report):
    config = config_at(tmp_path)
    write_reports(report, config)
    write_reports(report, config)

    assert len(list(config.report_dir.glob("*.md"))) == 2


def test_a_fresh_install_has_not_run_today(tmp_path):
    store = Store(tmp_path / "seen.db")

    assert already_ran_today(store, DAY) is False


def test_a_finished_run_marks_the_day_as_done(tmp_path):
    store = Store(tmp_path / "seen.db")
    store.mark_run(DAY)

    assert already_ran_today(store, DAY) is True


def test_yesterdays_run_does_not_count_as_todays(tmp_path):
    """The laptop may have been off for a day; that must not skip a morning."""
    store = Store(tmp_path / "seen.db")
    store.mark_run(date(2026, 9, 9))

    assert already_ran_today(store, DAY) is False
