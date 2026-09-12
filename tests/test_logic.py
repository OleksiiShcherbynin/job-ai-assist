import json
from pathlib import Path

import pytest

from core.logic import rejection_reason
from core.models import CandidateProfile, SearchPreferences, Vacancy

FIXTURES = Path(__file__).parent / "fixtures"

# Seniority levels above the candidate. Kept here rather than in the fixture so a
# test failure points at the rule being tested, not at data.
SENIORITY_DEAL_BREAKERS = ["senior", "medior", "architect"]


@pytest.fixture
def profile() -> CandidateProfile:
    return CandidateProfile(title="Software Developer", seniority="Junior")


@pytest.fixture
def prefs() -> SearchPreferences:
    return SearchPreferences(
        desired_roles=["junior", "student"],
        deal_breakers=SENIORITY_DEAL_BREAKERS,
    )


@pytest.fixture(scope="module")
def reference_vacancies() -> list[dict]:
    """Twenty real postings the user hand-picked as good matches for himself.

    They are the ground truth for the filter: rejecting any of them is a bug by
    definition, because the user already decided they fit.
    """
    return json.loads((FIXTURES / "reference_vacancies.json").read_text(encoding="utf-8"))


def test_deal_breaker_in_body_does_not_reject_a_junior_role(prefs, profile):
    """'senior' in the body describes a colleague, not the role on offer."""
    vacancy = Vacancy(
        role="Junior Specialist, Data & Technology",
        raw_text="Experimentovanie s AI: pod vedenim seniorneho kolegu budes testovat AI nastroje.",
    )

    assert rejection_reason(vacancy, prefs, profile) is None


def test_deal_breaker_in_title_still_rejects(prefs, profile):
    """Scoping to the title must not disarm the rule entirely."""
    vacancy = Vacancy(
        role="Senior IT Server Administrator (Azure)",
        raw_text="We are looking for an experienced administrator.",
    )

    assert rejection_reason(vacancy, prefs, profile) == "deal-breaker: 'senior'"


def test_no_reference_vacancy_is_rejected(prefs, profile, reference_vacancies):
    rejected = []
    for reference in reference_vacancies:
        vacancy = Vacancy(role=reference["title"], raw_text=reference["text"])
        reason = rejection_reason(vacancy, prefs, profile)
        if reason:
            rejected.append(f"{reference['title'][:60]} -> {reason}")

    assert rejected == [], "filter rejected vacancies the user picked himself:\n" + "\n".join(rejected)
