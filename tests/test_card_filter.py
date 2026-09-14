import pytest

from core.logic import card_rejection_reason
from core.models import SearchPreferences, VacancyCard


def card(**overrides) -> VacancyCard:
    base = dict(
        offer_id="O5000001",
        title="Junior Data Engineer",
        url="https://www.profesia.sk/praca/acme/O5000001",
        company="ACME",
    )
    return VacancyCard(**{**base, **overrides})


@pytest.fixture
def prefs() -> SearchPreferences:
    return SearchPreferences(desired_roles=["junior"], deal_breakers=["senior", "architect"])


def test_clean_card_survives(prefs):
    assert card_rejection_reason(card(), prefs) is None


def test_deal_breaker_in_title_rejects(prefs):
    reason = card_rejection_reason(card(title="Senior IT Server Administrator"), prefs)

    assert reason == "deal-breaker: 'senior'"


def test_deal_breaker_matching_ignores_slovak_diacritics(prefs):
    prefs.deal_breakers = ["veduci"]

    assert card_rejection_reason(card(title="Vedúci vývojár"), prefs) is not None


def test_a_deal_breaker_matches_inflected_forms(prefs):
    """Slovak inflects: one entry has to cover lektor/lektora/lektorka."""
    prefs.deal_breakers = ["lektor"]

    for title in ("Lektor programovania", "Lektorka v centre vzdelávania", "Hľadáme lektora"):
        assert card_rejection_reason(card(title=title), prefs) is not None, title


def test_a_deal_breaker_does_not_match_inside_another_word(prefs):
    """'lektor' sits inside 'kolektor'. A data-collector role must survive a
    ban on teaching, the same way a junior ad survives a ban on 'senior'."""
    prefs.deal_breakers = ["lektor"]

    assert card_rejection_reason(card(title="Kolektor dát"), prefs) is None
    assert card_rejection_reason(card(title="Selektor produktov"), prefs) is None


@pytest.mark.parametrize("title", [
    "IT Analytik Medior",
    "Implementation Engineer (Mid/Senior)",
    "Lead Architect (Solution Architect DevOps Technologies)",
    "Medior/Senior Linux OPS Administrátor/ka",
    "Mzdový účtovník/účtovníčka – Senior | Bratislava",
    "Product Manager, Senior - Insurance Services (f/m)",
    "Senior Biznis Konzultant (Consulting & Thought Leadership)",
    "Senior Data Analyst – BI & Reporting",
    "Senior Network Engineer / Senior sieťový technik (ISP)",
    "Software engineer full-stack (medior/senior)",
    "Technical Architect (.NET, .NET Core, TOGAF)",
])
def test_the_titles_rejected_on_a_live_run_are_still_rejected(prefs, title):
    """Word-boundary matching must not quietly let seniors back in: these are
    the eleven the filter caught on 2026-09-11, with that run's own list."""
    prefs.deal_breakers = ["senior", "medior", "architect"]

    assert card_rejection_reason(card(title=title), prefs) is not None


def test_required_title_keywords_are_off_by_default(prefs):
    """Losing 'IT konzultant' costs more than letting a bad match through: a bad
    match just scores low, a missed one is never seen."""
    assert card_rejection_reason(card(title="IT konzultant"), prefs) is None


def test_required_title_keywords_reject_when_switched_on(prefs):
    prefs.require_title_keywords = ["junior", "student"]

    assert card_rejection_reason(card(title="IT konzultant"), prefs) is not None
    assert card_rejection_reason(card(title="Junior Data Engineer"), prefs) is None


def test_monthly_pay_below_the_monthly_floor_rejects(prefs):
    prefs.min_salary_month = 800
    low = card(salary_min=400.0, salary_max=600.0, salary_period="month")

    assert card_rejection_reason(low, prefs) == "pay 600.0 EUR/month below floor 800.0"


def test_hourly_pay_is_never_measured_against_the_monthly_floor(prefs):
    """7 EUR/hod. is a normal student rate, not a rejection at a 800 EUR/mo floor."""
    prefs.min_salary_month = 800
    hourly = card(salary_min=7.0, salary_max=7.0, salary_period="hour")

    assert card_rejection_reason(hourly, prefs) is None


def test_hourly_floor_applies_to_hourly_pay(prefs):
    prefs.min_salary_hour = 8
    hourly = card(salary_min=6.12, salary_max=None, salary_period="hour")

    assert card_rejection_reason(hourly, prefs) is not None


def test_open_ended_pay_is_judged_on_its_lower_bound(prefs):
    prefs.min_salary_month = 1000
    open_ended = card(salary_min=1550.0, salary_max=None, salary_period="month")

    assert card_rejection_reason(open_ended, prefs) is None


def test_an_implausible_monthly_figure_is_treated_as_unknown(prefs):
    """Profesia carries a Zurich Insurance internship listed at '8 EUR/mesiac'.
    The employer picked the wrong unit; a pay floor must not reject a real
    vacancy over an obvious data error."""
    prefs.min_salary_month = 900
    mislabelled = card(title="Internship at Underwriting Service",
                       salary_min=8.0, salary_max=8.0, salary_period="month")

    assert card_rejection_reason(mislabelled, prefs) is None


def test_a_genuinely_low_monthly_wage_still_rejects(prefs):
    prefs.min_salary_month = 900
    low = card(salary_min=600.0, salary_max=600.0, salary_period="month")

    assert card_rejection_reason(low, prefs) is not None


def test_missing_pay_is_not_a_rejection(prefs):
    """Absence of data is not evidence against; let the LLM judge the posting."""
    prefs.min_salary_month = 1000

    assert card_rejection_reason(card(), prefs) is None
