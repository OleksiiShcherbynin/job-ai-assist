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
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from app.config import RunConfig, load_config
from app.gemini import GeminiJudge
from app.pacing import Pacer
from app.pipeline import Pipeline
from app.report import LANGUAGES, RunReport, render_markdown
from app.store import Store
from core.models import CandidateProfile
from local_connectors.llm import extract
from local_connectors.resume_loader import load_resume_text
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
    load_dotenv(override=False)

    if not os.environ.get("GOOGLE_API_KEY"):
        log.error("GOOGLE_API_KEY is not set; pass it with --env-file or the environment")
        return 2

    config = load_config(args.config)

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
                log.info("seen %d, scored %d, filtered %d, unprocessed %d",
                         report.seen, len(report.judged), len(report.rejected), len(report.failures))
            except Exception:
                log.exception("the run failed; will try again on the next check")

        if args.once:
            return 0
        time.sleep(IDLE_CHECK_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
