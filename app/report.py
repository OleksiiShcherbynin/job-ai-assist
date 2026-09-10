"""The one artefact the user actually reads.

Markdown, two files a day — one English, one Russian — meant to be skimmed in
the editor: strong matches first, weak ones after them, everything the filter
threw away folded into a collapsed block so a wrong rule is one click from
being noticed.

Both languages come out of a single model call, so the second file costs output
tokens rather than another request against a 20-a-day quota.
"""

from dataclasses import dataclass, field
from datetime import date

from core.models import VacancyCard

LANGUAGES = ("en", "ru")


@dataclass
class Judged:
    card: VacancyCard
    score: int
    stage: str
    """'final' if the judge saw the full resume, 'rough' if only a summary."""
    reasons: list[str] = field(default_factory=list)
    reasons_ru: list[str] = field(default_factory=list)

    def reasons_in(self, language: str) -> list[str]:
        # Falling back beats printing a vacancy with no explanation at all.
        return (self.reasons_ru or self.reasons) if language == "ru" else self.reasons


@dataclass
class Rejected:
    card: VacancyCard
    reason: str


@dataclass
class Failure:
    title: str
    url: str
    error: str


@dataclass
class RunReport:
    day: date
    seen: int
    judged: list[Judged] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)


_PHRASES = {
    "en": {
        "title": "Vacancies",
        "counts": "Seen {seen} · scored {scored} · above {min_score}: {strong} · filtered out {rejected}",
        "nothing": "Nothing scored above the threshold today.",
        "rough_note": "_rough score only — did not reach the final judge_",
        "open": "Open the posting",
        "below": "Scored below {min_score} ({count})",
        "filtered": "Filtered out before scoring ({count})",
        "failed": "Not processed ({count})",
    },
    "ru": {
        "title": "Вакансии",
        "counts": "Просмотрено {seen} · оценено {scored} · выше {min_score}: {strong} · отсеяно {rejected}",
        "nothing": "Сегодня ничего не набрало проходной балл.",
        "rough_note": "_только грубая оценка — до финального разбора не дошла_",
        "open": "Открыть вакансию",
        "below": "Ниже порога {min_score} ({count})",
        "filtered": "Отсеяно до оценки ({count})",
        "failed": "Не обработано ({count})",
    },
}


def _facts(card: VacancyCard) -> str:
    parts = [card.company or "—"]
    if card.allows_home_office:
        parts.append("home office")
    if card.salary_text:
        parts.append(card.salary_text)
    return " · ".join(parts)


def _entry(item: Judged, phrases: dict[str, str], language: str) -> list[str]:
    lines = [f"## {item.score} · {item.card.title}", "", _facts(item.card), ""]
    lines += [f"- {reason}" for reason in item.reasons_in(language)]
    if item.stage != "final":
        lines.append(f"- {phrases['rough_note']}")
    lines += ["", f"[{phrases['open']}]({item.card.url})", ""]
    return lines


def render_markdown(report: RunReport, min_score: int, language: str = "en") -> str:
    if language not in _PHRASES:
        raise ValueError(f"no phrasing for language {language!r}; known: {', '.join(LANGUAGES)}")
    phrases = _PHRASES[language]

    ranked = sorted(report.judged, key=lambda item: item.score, reverse=True)
    strong = [item for item in ranked if item.score >= min_score]
    weak = [item for item in ranked if item.score < min_score]

    lines = [
        f"# {phrases['title']} — {report.day.isoformat()}",
        "",
        phrases["counts"].format(
            seen=report.seen,
            scored=len(report.judged),
            min_score=min_score,
            strong=len(strong),
            rejected=len(report.rejected),
        ),
        "",
    ]

    if strong:
        for item in strong:
            lines += _entry(item, phrases, language)
    else:
        lines += [phrases["nothing"], ""]

    if weak:
        summary = phrases["below"].format(min_score=min_score, count=len(weak))
        lines += ["---", "", f"<details><summary>{summary}</summary>", ""]
        for item in weak:
            lines += _entry(item, phrases, language)
        lines += ["</details>", ""]

    if report.rejected:
        summary = phrases["filtered"].format(count=len(report.rejected))
        lines += ["---", "", f"<details><summary>{summary}</summary>", ""]
        # One line each: enough to spot a rule that is cutting too much.
        lines += [
            f"- [{item.card.title}]({item.card.url}) — {item.reason}"
            for item in report.rejected
        ]
        lines += ["", "</details>", ""]

    if report.failures:
        lines += ["---", "", f"### {phrases['failed'].format(count=len(report.failures))}", ""]
        lines += [f"- [{item.title}]({item.url}) — {item.error}" for item in report.failures]
        lines += [""]

    return "\n".join(lines)
