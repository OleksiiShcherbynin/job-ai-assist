import unicodedata
from datetime import datetime

from core.models import (
    CandidateProfile,
    SearchPreferences,
    Vacancy,
    VacancyCard,
)


def strip_accents(s: str) -> str:
    """Remove diacritics: 'študent' -> 'student', 'príležitosť' -> 'prilezitost'."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _searchable_text(v: Vacancy) -> str:
    """Combine all searchable fields of a vacancy into one normalized string."""
    parts = [
        v.raw_text or "",
        v.role or "",
        v.company or "",
        v.additional_info or "",
    ]
    return strip_accents(" ".join(parts).lower())


def _title_text(v: Vacancy) -> str:
    """Only the role title, normalized.

    Deal-breakers are matched here rather than against the whole posting: in the
    body, seniority words describe colleagues ('pod vedenim seniorneho kolegu')
    or reassure the reader ('nemusis byt senior'), and matching them there
    rejects the junior roles they are advertising.
    """
    return strip_accents((v.role or "").lower())


def card_rejection_reason(card: VacancyCard, prefs: SearchPreferences) -> str | None:
    """Reject from the listing row alone — before a page load or an LLM call.

    Everything here is decided by data Profesia already shows in the search
    results, so a rejection at this stage costs nothing.
    """
    title = strip_accents(card.title.lower())

    for word in prefs.deal_breakers:
        if strip_accents(word.lower()) in title:
            return f"deal-breaker: {word!r}"

    if prefs.require_title_keywords:
        wanted = [strip_accents(keyword.lower()) for keyword in prefs.require_title_keywords]
        if not any(keyword in title for keyword in wanted):
            return f"title matches none of {prefs.require_title_keywords}"

    floor = {
        "month": prefs.min_salary_month,
        "hour": prefs.min_salary_hour,
    }.get(card.salary_period)
    if floor is not None:
        # 'Od 1 550' quotes a lower bound and no maximum; judge it on that figure.
        top = card.salary_max if card.salary_max is not None else card.salary_min
        if top is not None and top < floor:
            return f"pay {top} EUR/{card.salary_period} below floor {float(floor)}"

    return None


def rejection_reason(
    v: Vacancy,
    prefs: SearchPreferences,
    profile: CandidateProfile,
) -> str | None:
    text = _searchable_text(v)
    title = _title_text(v)

    for word in prefs.deal_breakers:
        needle = strip_accents(word.lower())
        if needle in title:
            return f"deal-breaker: {word!r}"

    if prefs.work_formats and v.work_format and v.work_format not in prefs.work_formats:
        return f"format {v.work_format.value} not among the desired"

    if prefs.min_salary and v.salary_max and v.salary_max < prefs.min_salary:
        return f"salary range {v.salary_max} < minimum {prefs.min_salary}"

    for skill in prefs.must_have:
        needle = strip_accents(skill.lower())
        if needle not in text:
            return f"no mandatory: {skill!r}"

    return None


def missing_fields(v: Vacancy, prefs: SearchPreferences) -> list[str]:

    gaps: list[str] = []
    if prefs.min_salary and not (v.salary_min or v.salary_max):
        gaps.append("salary range")
    if prefs.work_formats and v.work_format is None:
        gaps.append("work format")
    if prefs.locations and not v.location:
        gaps.append("location")
    if not v.tech_stack:
        gaps.append("tech stack")
    return gaps


def has_enough_info(v: Vacancy, prefs: SearchPreferences, max_gap: int = 2) -> bool:
    
    return len(missing_fields(v, prefs)) <= max_gap


def apply_reply_updates(v: Vacancy) -> None:

    v.salary_min = v.salary_min
    v.salary_max = v.salary_max
    v.work_format = v.work_format
    v.location = v.location
