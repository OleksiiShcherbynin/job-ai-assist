"""Run settings read from config.toml.

Criteria live here rather than in code so they can be tuned without a rebuild —
which matters because they are meant to be tuned: the reference set showed the
original ones rejecting vacancies the user had picked himself.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from core.models import SearchPreferences

DEFAULT_LIST_URL = (
    "https://www.profesia.sk/praca/bratislava/?count_days=1"
    "&education_levels[]=2&education_levels[]=8&education_levels[]=3"
    "&education_levels[]=5&education_levels[]=9"
    "&jobtypes[]=1&jobtypes[]=2&jobtypes[]=4&jobtypes[]=32"
    "&search_anywhere=student%2C+IT%2C+junior&sort_by=relevance"
)

# Google stopped publishing free-tier limits, so a model nobody configured is
# assumed to be on the tightest tier seen in the wild rather than a generous one.
CAUTIOUS_QUOTA_RPM = 10
CAUTIOUS_QUOTA_RPD = 250


@dataclass(frozen=True)
class ModelQuota:
    rpm: int
    rpd: int


@dataclass(frozen=True)
class RunConfig:
    resume_path: Path
    state_path: Path
    report_dir: Path
    list_url: str
    start_page: int
    stop_page: int
    call_budget: int
    min_score: int
    preferences: SearchPreferences
    quotas: dict[str, ModelQuota] = field(default_factory=dict)

    def quota_for(self, model: str) -> ModelQuota:
        return self.quotas.get(model, ModelQuota(CAUTIOUS_QUOTA_RPM, CAUTIOUS_QUOTA_RPD))


def load_config(path: str | Path) -> RunConfig:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no config at {path}")

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    base = path.parent
    search = raw.get("search", {})
    run = raw.get("run", {})
    preferences = raw.get("preferences", {})

    def resolve(value: str) -> Path:
        # Relative paths are anchored to the config file, not the working
        # directory: the container starts runs from wherever it likes.
        candidate = Path(value)
        return candidate if candidate.is_absolute() else base / candidate

    return RunConfig(
        resume_path=resolve(run.get("resume_path", "data/resume.pdf")),
        state_path=resolve(run.get("state_path", "state/seen.db")),
        report_dir=resolve(run.get("report_dir", "reports")),
        list_url=search.get("list_url", DEFAULT_LIST_URL),
        start_page=int(search.get("start_page", 1)),
        stop_page=int(search.get("stop_page", 6)),
        call_budget=int(run.get("call_budget", 200)),
        min_score=int(run.get("min_score", 0)),
        preferences=SearchPreferences(
            desired_roles=preferences.get("desired_roles", []),
            deal_breakers=preferences.get("deal_breakers", []),
            must_have=preferences.get("must_have", []),
            locations=preferences.get("locations", []),
            require_title_keywords=preferences.get("require_title_keywords", []),
            min_salary_month=preferences.get("min_salary_month"),
            min_salary_hour=preferences.get("min_salary_hour"),
        ),
        quotas={
            model: ModelQuota(rpm=int(limits["rpm"]), rpd=int(limits["rpd"]))
            for model, limits in raw.get("quota", {}).items()
        },
    )
