"""The one artefact the user actually reads.

Markdown, one file a day, meant to be skimmed in the editor: strong matches
first, weak ones after them, everything the filter threw away folded into a
collapsed block so a wrong rule is one click from being noticed.
"""

from dataclasses import dataclass, field
from datetime import date

from core.models import VacancyCard


@dataclass
class Judged:
    card: VacancyCard
    score: int
    reasons: list[str]
    stage: str
    """'final' if the judge saw the full resume, 'rough' if only a summary."""


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


def _facts(card: VacancyCard) -> str:
    parts = [card.company or "—"]
    if card.allows_home_office:
        parts.append("home office")
    if card.salary_text:
        parts.append(card.salary_text)
    return " · ".join(parts)


def _entry(item: Judged) -> list[str]:
    lines = [
        f"## {item.score} · {item.card.title}",
        "",
        f"{_facts(item.card)}",
        "",
    ]
    lines += [f"- {reason}" for reason in item.reasons]
    if item.stage != "final":
        lines.append("- _rough score only — did not reach the final judge_")
    lines += ["", f"[Open the posting]({item.card.url})", ""]
    return lines


def render_markdown(report: RunReport, min_score: int) -> str:
    ranked = sorted(report.judged, key=lambda item: item.score, reverse=True)
    strong = [item for item in ranked if item.score >= min_score]
    weak = [item for item in ranked if item.score < min_score]

    lines = [
        f"# Vacancies — {report.day.isoformat()}",
        "",
        f"Seen {report.seen} · scored {len(report.judged)} · "
        f"above {min_score}: {len(strong)} · filtered out {len(report.rejected)}",
        "",
    ]

    if strong:
        for item in strong:
            lines += _entry(item)
    else:
        lines += ["Nothing scored above the threshold today.", ""]

    if weak:
        lines += ["---", "", f"<details><summary>Scored below {min_score} ({len(weak)})</summary>", ""]
        for item in weak:
            lines += _entry(item)
        lines += ["</details>", ""]

    if report.rejected:
        lines += [
            "---",
            "",
            f"<details><summary>Filtered out before scoring ({len(report.rejected)})</summary>",
            "",
        ]
        # One line each: enough to spot a rule that is cutting too much.
        lines += [
            f"- [{item.card.title}]({item.card.url}) — {item.reason}"
            for item in report.rejected
        ]
        lines += ["", "</details>", ""]

    if report.failures:
        lines += ["---", "", f"### Not processed ({len(report.failures)})", ""]
        lines += [f"- [{item.title}]({item.url}) — {item.error}" for item in report.failures]
        lines += [""]

    return "\n".join(lines)
