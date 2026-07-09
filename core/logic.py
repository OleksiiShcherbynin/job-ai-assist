import unicodedata
from datetime import datetime

from core.models import (
    CandidateProfile,
    SearchPreferences,
    Vacancy,
)


def _strip_accents(s: str) -> str:
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
    return _strip_accents(" ".join(parts).lower())


def rejection_reason(
    v: Vacancy,
    prefs: SearchPreferences,
    profile: CandidateProfile,
) -> str | None:
    text = _searchable_text(v)

    for word in prefs.deal_breakers:
        needle = _strip_accents(word.lower())
        if needle in text:
            return f"deal-breaker: {word!r}"

    if prefs.work_formats and v.work_format and v.work_format not in prefs.work_formats:
        return f"format {v.work_format.value} not among the desired"

    if prefs.min_salary and v.salary_max and v.salary_max < prefs.min_salary:
        return f"salary range {v.salary_max} < minimum {prefs.min_salary}"

    for skill in prefs.must_have:
        needle = _strip_accents(skill.lower())
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
