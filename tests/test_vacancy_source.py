from pathlib import Path

import pytest

from local_connectors.vacancy_source import parse_cards, parse_salary

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def listing_html() -> str:
    """A real Bratislava IT listing page, saved 2026-09-10."""
    return (FIXTURES / "profesia_listing.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cards(listing_html):
    return parse_cards(listing_html)


def test_parses_every_offer_on_the_page(cards):
    assert len(cards) == 20


def test_card_carries_identity_from_the_listing(cards):
    card = next(c for c in cards if c.offer_id == "O5356002")

    assert card.title == "IT konzultant/IT konzultantka"
    assert card.company == "MICROCOMP - Computersystém s.r.o."


def test_url_is_absolute_and_free_of_tracking_parameters(cards):
    card = next(c for c in cards if c.offer_id == "O5356002")

    assert card.url == "https://www.profesia.sk/praca/microcomp-computersystem/O5356002"


def test_home_office_hint_is_read_from_the_location(cards):
    with_hint = next(c for c in cards if c.offer_id == "O5356002")
    without_hint = next(c for c in cards if c.offer_id == "O5356455")

    assert with_hint.allows_home_office is True
    assert without_hint.allows_home_office is False


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1 800 EUR/mesiac", (1800.0, 1800.0, "month")),
        ("1 500 - 1 900 EUR/mesiac", (1500.0, 1900.0, "month")),
        ("Od 1 550 EUR/mesiac", (1550.0, None, "month")),
        ("400 - 800 EUR/mesiac", (400.0, 800.0, "month")),
        ("7 EUR/hod.", (7.0, 7.0, "hour")),
        ("Od 6,12 EUR/hod.", (6.12, None, "hour")),
        ("", (None, None, None)),
    ],
)
def test_parses_salary_forms_profesia_actually_uses(text, expected):
    assert parse_salary(text) == expected


def test_hourly_and_monthly_pay_are_not_comparable_as_plain_numbers(cards):
    """A brigada at 7 EUR/hod. must not look cheaper than 400 EUR/mesiac."""
    hourly = parse_salary("7 EUR/hod.")
    monthly = parse_salary("400 - 800 EUR/mesiac")

    assert hourly[2] != monthly[2]
