"""Several resumes for one candidate.

The user keeps one resume per direction (software engineering, data analysis)
and decides himself which to send. The run only needs two things from the set:
one profile wide enough that the cheap scoring stage does not miss a vacancy
aimed at either direction, and, for each finalist, one resume for the judge to
read — two full resumes in one prompt would blur the judgement, and two calls
would halve the finalists.
"""

import re
from dataclasses import dataclass

from core.logic import mentions, strip_accents
from core.models import CandidateProfile, Vacancy

_STEM = 5
"""Title words are matched by their first letters so an English resume title
still meets a Slovak posting: 'analy' is in both 'analyst' and 'analytik'."""

_SENIORITY_WORDS = {"junior", "medior", "senior", "trainee", "student", "intern", "stazista"}
"""Both of the user's resumes say 'junior'; the word tells the two apart no
better than it tells a vacancy apart."""

_TITLE_WEIGHT = 3
"""A title word names the direction outright; a stack item only hints at it."""


@dataclass(frozen=True)
class Resume:
    name: str
    """The file name, which is how the report tells the user which to send."""
    text: str
    profile: CandidateProfile


def merge_profiles(profiles: list[CandidateProfile]) -> CandidateProfile:
    """One profile that claims everything any of the resumes claims.

    Lists are joined without repeats, in resume order; scalar fields come from
    the first resume that states them. Done in code rather than by asking the
    model to read both: a model handed two titles tends to pick one.
    """
    first, *rest = profiles
    merged = first.model_copy(deep=True)

    for profile in rest:
        merged.full_name = merged.full_name or profile.full_name
        merged.title = _join_titles(merged.title, profile.title)
        merged.location = merged.location or profile.location
        merged.seniority = merged.seniority or profile.seniority
        merged.summary = merged.summary or profile.summary
        merged.additional_info = merged.additional_info or profile.additional_info
        if profile.years_experience is not None:
            merged.years_experience = max(merged.years_experience or 0, profile.years_experience)
        merged.tech_stack = _union(merged.tech_stack, profile.tech_stack, key=_fold)
        merged.languages = _union(merged.languages, profile.languages,
                                  key=lambda lang: _fold(lang.language_code or ""))
        merged.experience = _union(merged.experience, profile.experience,
                                   key=lambda entry: (_fold(entry.company or ""), _fold(entry.role or "")))
        merged.education = _union(merged.education, profile.education,
                                  key=lambda item: (_fold(item.degree or ""), _fold(item.field or ""),
                                                    _fold(item.institution or "")))
    return merged


def pick_resume(vacancy: Vacancy, resumes: list[Resume]) -> Resume:
    """The resume whose direction the posting speaks to most.

    Two signals, both measured on real postings before being trusted:
    - the resume's title words in the vacancy's TITLE. Not in its body: there
      'databáza' and 'analyzovať' turn up in nearly every Slovak IT ad and sent
      a Java developer role to the analyst resume;
    - skills only this resume claims, found anywhere in the posting. Skills on
      every resume (Python, SQL, Git) cannot tell them apart.
    A tie goes to the first resume in the config, so the order there is the
    user's default. No model call: this only chooses what the judge reads.
    """
    title = _fold(vacancy.role or "")
    body = _fold(" ".join([vacancy.role or "", " ".join(vacancy.tech_stack), vacancy.raw_text]))
    skills = [set(_skill_terms(resume.profile)) for resume in resumes]

    def weight(index: int) -> int:
        others = set().union(*(terms for other, terms in enumerate(skills) if other != index))
        own = skills[index] - others
        skill_hits = sum(1 for term in own if _whole_word(body, term))
        title_hits = sum(1 for stem in _title_stems(resumes[index].profile.title) if mentions(title, stem))
        return skill_hits + _TITLE_WEIGHT * title_hits

    best = max(range(len(resumes)), key=weight)
    return resumes[best]


def _skill_terms(profile: CandidateProfile) -> list[str]:
    """Stack items as they would be written in a posting.

    Versions go ('Java 21' is advertised as 'Java'), parentheses go ('C (C99)'
    is 'C'), and 'TensorFlow/Keras' is two skills.
    """
    terms = []
    for item in profile.tech_stack:
        item = re.sub(r"\([^)]*\)", " ", _fold(item))
        for part in item.split("/"):
            words = [word for word in part.split() if not any(ch.isdigit() for ch in word)]
            if words:
                terms.append(" ".join(words))
    return terms


def _whole_word(haystack: str, term: str) -> bool:
    """Whole words for skills, unlike the prefixes used for titles: a prefix
    'c' matches every word starting with c, and 'sql' would match 'sqlite'."""
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", haystack) is not None


def _title_stems(title: str | None) -> list[str]:
    words = [word for word in _fold(title or "").replace("/", " ").split() if word.isalpha()]
    return [word[:_STEM] for word in words if len(word) >= 4 and word not in _SENIORITY_WORDS]


def _join_titles(first: str | None, second: str | None) -> str | None:
    if not first or not second:
        return first or second
    if _fold(second) in [_fold(part) for part in first.split(" / ")]:
        return first
    return f"{first} / {second}"


def _union(left: list, right: list, key) -> list:
    seen = {key(item) for item in left}
    extra = []
    for item in right:
        if key(item) not in seen:
            seen.add(key(item))
            extra.append(item)
    return list(left) + extra


def _fold(text: str) -> str:
    return strip_accents(text.lower()).strip()
