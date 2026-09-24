"""Entry point for the daily run.

Designed for a laptop rather than a server. The trigger is "first launch of the
day", not a clock time: a machine that was closed at 06:00 and opened at 14:00
should still get its report, and one opened twice should not pay for two runs.
"""

import argparse
import logging
import os
import re
import sys
import threading
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from app.bot import Bot
from app.config import RunConfig, load_config
from app.gemini import GeminiJudge
from app.pacing import Pacer
from app.pipeline import Pipeline
from app.report import LANGUAGES, RunReport, render_markdown, render_telegram_summary
from app.store import Store
from core.models import CandidateProfile
from core.ports import Messenger
from local_connectors.llm import extract, is_account_error
from local_connectors.resume_loader import load_resume_text
from local_connectors.telegram import TelegramClient, redact
from local_connectors.vacancy_source import ProfesiaSource

log = logging.getLogger("daily-run")

IDLE_CHECK_SECONDS = 15 * 60
"""Short enough that a laptop woken at any hour gets its report soon after,
and short enough to survive suspend without arithmetic about wall clocks."""

RESUME_INSTRUCTION = (
    "Extract the candidate's profile from the resume text into CandidateProfile. "
    "Use None or an empty list for anything the resume does not state."
)


def already_ran_today(store: Store, today: date) -> bool:
    return store.last_run_date() == today


def write_reports(report: RunReport, config: RunConfig) -> list[Path]:
    config.report_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for language in LANGUAGES:
        path = config.report_dir / f"{report.day.isoformat()}.{language}.md"
        path.write_text(
            render_markdown(report, config.min_score, language),
            encoding="utf-8",
            newline="\n",
        )
        written.append(path)
    return written


def telegram_settings() -> tuple[str | None, int | None]:
    """Token and owner chat id. Both are secrets-adjacent, so only the
    environment carries them — never config.toml."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
    raw_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if re.fullmatch(r"-?\d+", raw_chat) else None
    return token, chat_id


class RedactingFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.secret)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.secret)
        return True


def install_redaction(secret: str) -> RedactingFilter:
    """Masks the secret in every line any handler writes, tracebacks included.

    The Telegram client already keeps the token out of its errors; this is the
    last line for whatever else might quote a URL. Handler filters rather than a
    logger filter, because records from child loggers skip the root's filters.
    """
    redacting = RedactingFilter(secret)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redacting)
    return redacting


def queue_report(config: RunConfig, report: RunReport) -> None:
    """Queues the day's summaries, but only for a run that finished.

    A run cut short by a refusing API is not marked done and repeats on every
    check; queueing it would send a message every fifteen minutes.
    """
    store = Store(config.state_path)
    try:
        if store.last_run_date() != report.day:
            return
        store.queue_delivery(report.day, {
            language: render_telegram_summary(report, config.min_score, language)
            for language in LANGUAGES
        })
    finally:
        store.close()


def deliver(config: RunConfig, messenger: Messenger, chat_id: int | None) -> None:
    """Drains the queue. A delivery problem never becomes a run problem."""
    store = Store(config.state_path)
    try:
        Bot(messenger, store, config.report_dir, chat_id).deliver_pending()
    except Exception as error:
        log.warning("could not send the report to Telegram; will retry on the next check (%s)", error)
    finally:
        store.close()


def start_listener(token: str, chat_id: int | None, config: RunConfig,
                   wake: threading.Event) -> threading.Thread:
    def listen() -> None:
        # Opened inside the thread: an sqlite3 connection may not cross threads.
        store = Store(config.state_path)
        client = TelegramClient(token)
        try:
            Bot(client, store, config.report_dir, chat_id, wake=wake).listen(threading.Event())
        except Exception:
            log.exception("the Telegram listener stopped")
        finally:
            client.close()
            store.close()

    thread = threading.Thread(target=listen, name="telegram", daemon=True)
    thread.start()
    return thread


def _clean_resume(text: str) -> str:
    text = text.replace("", "-")
    text = re.sub(r"\n{3,}", "\n\n ", text)
    return re.sub(r" {2,}", " ", text).strip()


def _summaries(profile: CandidateProfile, config: RunConfig) -> tuple[str, str]:
    profile_summary = f"{profile.title}, {profile.seniority}, stack: {profile.tech_stack}"
    prefs_summary = f"roles: {config.preferences.desired_roles}"
    return profile_summary, prefs_summary


def run_today(config: RunConfig, today: date) -> RunReport:
    resume_text = _clean_resume(load_resume_text(str(config.resume_path)))
    profile = extract(resume_text, CandidateProfile, RESUME_INSTRUCTION, model=config.models.extract)
    profile_summary, prefs_summary = _summaries(profile, config)

    store = Store(config.state_path)
    source = ProfesiaSource(
        list_url=config.list_url,
        start_page=config.start_page,
        stop_page=config.stop_page,
    )
    try:
        pipeline = Pipeline(
            config=config,
            source=source,
            store=store,
            pacer=Pacer(store, config),
            llm=GeminiJudge(config, profile_summary, prefs_summary, resume_text),
            resume_text=resume_text,
        )
        report = pipeline.run(today)
    finally:
        source.close()
        store.close()

    for path in write_reports(report, config):
        log.info("wrote %s", path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score today's Bratislava IT vacancies.")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--once", action="store_true",
                        help="check once and exit instead of waiting for the next day")
    parser.add_argument("--force", action="store_true",
                        help="run even if today is already marked done")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO. Telegram's URLs carry the bot token,
    # and long polling would add a line a minute; the other request lines were
    # noise already.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(override=False)

    if not os.environ.get("GOOGLE_API_KEY"):
        log.error("GOOGLE_API_KEY is not set; pass it with --env-file or the environment")
        return 2

    config = load_config(args.config)

    token, chat_id = telegram_settings()
    telegram: Messenger | None = None
    wake = threading.Event()
    if token:
        install_redaction(token)
        telegram = TelegramClient(token)
        if chat_id is None:
            log.info("TELEGRAM_CHAT_ID is not set: send /start to the bot to learn it; "
                     "reports are queued until then")
        if not args.once:
            start_listener(token, chat_id, config, wake)
    else:
        log.info("TELEGRAM_BOT_TOKEN is not set; the Telegram bot is off")

    failed = False

    while True:
        today = date.today()
        store = Store(config.state_path)
        done = already_ran_today(store, today)
        store.close()

        if done and not args.force:
            log.info("%s already done", today)
        else:
            log.info("running for %s", today)
            try:
                report = run_today(config, today)
                failed = False
                log.info("seen %d, scored %d, filtered %d, unprocessed %d",
                         report.seen, len(report.judged), len(report.rejected), len(report.failures))
            except Exception as error:
                failed = True
                if is_account_error(error):
                    # Diagnosed and actionable: say what to do, not where it broke.
                    # Nobody watches this log live, and a stack trace buries the
                    # one sentence that matters.
                    log.error(
                        "the Gemini API refuses every request for this key — check the "
                        "project's billing at https://ai.studio/projects. Nothing will "
                        "run until that is resolved. (%s)",
                        str(error)[:200],
                    )
                    return 3
                log.exception("the run failed; will try again on the next check")
            else:
                if telegram is not None:
                    try:
                        queue_report(config, report)
                    except Exception:
                        log.exception("could not queue the report for Telegram")

        if telegram is not None:
            deliver(config, telegram, chat_id)

        if args.once:
            # In the loop a failed run just waits for the next check. With --once
            # there is no next check inside this process, so a caller must be able
            # to tell a finished run from a broken one.
            return 1 if failed else 0
        # A chosen language wakes the loop early, so queued reports need not
        # wait out the rest of the interval.
        wake.wait(IDLE_CHECK_SECONDS)
        wake.clear()


if __name__ == "__main__":
    sys.exit(main())
