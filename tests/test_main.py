import logging
from datetime import date

import pytest

from app.config import ModelQuota, RunConfig
from app.main import (RedactingFilter, already_ran_today, install_redaction, telegram_settings,
                      write_reports)
from app.report import Judged, RunReport
from app.store import Store
from core.models import SearchPreferences, VacancyCard

DAY = date(2026, 9, 10)


@pytest.fixture(autouse=True)
def no_telegram_unless_asked(monkeypatch):
    """The developer's shell may carry real Telegram settings; tests never use them."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    yield
    for handler in logging.getLogger().handlers:
        for installed in [f for f in handler.filters if isinstance(f, RedactingFilter)]:
            handler.removeFilter(installed)


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


DEPLETED = ("429 RESOURCE_EXHAUSTED. {'error': {'message': 'Your prepayment credits are "
            "depleted. Please go to AI Studio at https://ai.studio/projects'}}")


def test_a_dead_account_is_reported_in_one_line_not_a_traceback(tmp_path, monkeypatch, caplog):
    """This runs unattended. A wall of stack trace in a log nobody watches is
    worse than one sentence naming the page that fixes it."""
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))

    def refuse(config, today):
        raise RuntimeError(DEPLETED)

    monkeypatch.setattr(main_module, "run_today", refuse)

    code = main_module.main(["--once"])

    assert code == 3, "a billing problem is not the same exit as success"
    assert "ai.studio" in caplog.text
    assert "Traceback" not in caplog.text


def test_an_ordinary_failure_keeps_its_traceback_and_reports_failure(tmp_path, monkeypatch, caplog):
    """Anything undiagnosed keeps its stack trace. In --once there is no
    tomorrow inside this process, so exiting 0 would tell a script it worked."""
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "run_today",
                        lambda config, today: (_ for _ in ()).throw(RuntimeError("something odd")))

    code = main_module.main(["--once"])

    assert code == 1
    assert "something odd" in caplog.text


def test_a_successful_once_run_reports_success(tmp_path, monkeypatch, report):
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "run_today", lambda config, today: report)

    assert main_module.main(["--once"]) == 0


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


class RecordingMessenger:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, chat_id, html, buttons=None):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append(("message", chat_id, html))

    def send_document(self, chat_id, path, caption=None):
        self.sent.append(("document", chat_id, path.name))


def with_telegram(monkeypatch, tmp_path, messenger, report, finished=True):
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1001")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "TelegramClient", lambda token: messenger)
    monkeypatch.setattr(main_module, "start_listener",
                        lambda *a: pytest.fail("--once must not start the listener"))

    def run(config, today):
        write_reports(report, config)
        if finished:
            store = Store(config.state_path)
            store.mark_run(report.day)
            store.close()
        return report

    monkeypatch.setattr(main_module, "run_today", run)
    store = Store(config_at(tmp_path).state_path)
    store.set_state("language", "en")
    store.close()
    return main_module


def test_a_finished_run_reaches_telegram_as_summary_and_file(tmp_path, monkeypatch, report):
    messenger = RecordingMessenger()
    main_module = with_telegram(monkeypatch, tmp_path, messenger, report)

    assert main_module.main(["--once"]) == 0

    assert messenger.sent[0][:2] == ("message", 1001)
    assert "Junior Data Engineer" in messenger.sent[0][2]
    assert messenger.sent[1] == ("document", 1001, "2026-09-10.en.md")


def test_an_unfinished_run_is_not_queued(tmp_path, monkeypatch, report):
    """A run cut short is not marked done and repeats every check; queueing it
    would send a message every fifteen minutes."""
    messenger = RecordingMessenger()
    main_module = with_telegram(monkeypatch, tmp_path, messenger, report, finished=False)

    main_module.main(["--once"])

    assert messenger.sent == []


def test_a_telegram_failure_does_not_fail_the_run(tmp_path, monkeypatch, report, caplog):
    messenger = RecordingMessenger(fail=True)
    main_module = with_telegram(monkeypatch, tmp_path, messenger, report)

    assert main_module.main(["--once"]) == 0
    assert "will retry" in caplog.text
    assert len(Store(config_at(tmp_path).state_path).pending_deliveries()) == 1


def test_without_a_token_the_bot_stays_off(tmp_path, monkeypatch, report):
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "TelegramClient", lambda token: pytest.fail("no token, no client"))
    monkeypatch.setattr(main_module, "run_today", lambda config, today: report)

    assert main_module.main(["--once"]) == 0
    assert Store(config_at(tmp_path).state_path).pending_deliveries() == []


def test_httpx_request_lines_are_kept_out_of_the_log(tmp_path, monkeypatch, report):
    """They carry the bot token in the URL, and long polling adds one a minute."""
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "run_today", lambda config, today: report)

    main_module.main(["--once"])

    assert logging.getLogger("httpx").level == logging.WARNING


def test_the_token_is_masked_in_every_log_line_and_traceback(caplog):
    install_redaction("fake-token")
    logger = logging.getLogger("anything")

    logger.error("GET https://api.telegram.org/bot%s/getUpdates", "fake-token")
    try:
        raise RuntimeError("failed at /botfake-token/sendMessage")
    except RuntimeError:
        logger.exception("boom")

    assert "fake-token" not in caplog.text
    assert "/bot***/getUpdates" in caplog.text


@pytest.mark.parametrize("raw_chat, expected", [("1001", 1001), ("-100200", -100200), ("", None), ("abc", None)])
def test_telegram_settings_come_from_the_environment(monkeypatch, raw_chat, expected):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " fake-token ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", raw_chat)

    assert telegram_settings() == ("fake-token", expected)


def test_an_empty_token_means_no_bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    assert telegram_settings() == (None, None)
