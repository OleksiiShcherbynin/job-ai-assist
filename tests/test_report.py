from datetime import date

import pytest

from app.report import (TELEGRAM_LIMIT, Failure, Judged, Rejected, RunReport,
                        render_markdown, render_telegram_summary, telegram_length)
from core.models import VacancyCard


def card(offer_id="O5000001", title="Junior Data Engineer", **extra) -> VacancyCard:
    base = dict(
        offer_id=offer_id,
        title=title,
        url=f"https://www.profesia.sk/praca/acme/{offer_id}",
        company="ACME",
        salary_text="1 300 - 1 600 EUR/mesiac",
        allows_home_office=True,
    )
    return VacancyCard(**{**base, **extra})


def report(**overrides) -> RunReport:
    base = dict(day=date(2026, 9, 10), seen=99, judged=[], rejected=[], failures=[])
    return RunReport(**{**base, **overrides})


@pytest.fixture
def two_scored() -> list[Judged]:
    return [
        Judged(card=card("O5000001", "Junior Data Engineer"), score=87,
               reasons=["stack matches", "student welcome"],
               reasons_ru=["стек совпадает", "готовы брать студента"], stage="final"),
        Judged(card=card("O5000002", "Student Data Analyst"), score=54,
               reasons=["adjacent field"], reasons_ru=["смежная область"], stage="rough"),
    ]


def test_the_header_counts_what_the_run_did(two_scored):
    text = render_markdown(report(seen=99, judged=two_scored), min_score=40)

    assert "2026-09-10" in text
    assert "99" in text


def test_the_best_match_comes_first(two_scored):
    text = render_markdown(report(judged=list(reversed(two_scored))), min_score=40)

    assert text.index("Junior Data Engineer") < text.index("Student Data Analyst")


def test_an_entry_carries_the_link_and_what_the_card_knew(two_scored):
    text = render_markdown(report(judged=two_scored), min_score=40)

    assert "https://www.profesia.sk/praca/acme/O5000001" in text
    assert "ACME" in text
    assert "1 300 - 1 600 EUR/mesiac" in text
    assert "stack matches" in text


def test_a_roughly_scored_vacancy_says_so(two_scored):
    """Not every vacancy reaches the judge; the report must not imply it did."""
    text = render_markdown(report(judged=two_scored), min_score=40)

    rough_section = text[text.index("Student Data Analyst"):]
    assert "rough" in rough_section.lower()


def test_vacancies_below_the_threshold_go_to_the_tail():
    weak = Judged(card=card("O5000003", "Upratovačka"), score=12, reasons=["not IT"], stage="rough")
    strong = Judged(card=card("O5000001", "Junior Data Engineer"), score=87, reasons=["fits"], stage="final")

    text = render_markdown(report(judged=[weak, strong]), min_score=40)

    assert text.index("Junior Data Engineer") < text.index("Upratovačka")


def test_rejected_vacancies_are_listed_with_their_reason():
    rejected = [Rejected(card=card("O5000009", "Senior Architect"), reason="deal-breaker: 'senior'")]

    text = render_markdown(report(rejected=rejected), min_score=40)

    assert "Senior Architect" in text
    assert "deal-breaker: 'senior'" in text
    assert "<details>" in text, "rejects belong in a collapsed block, not in the way"


def test_failures_are_reported_rather_than_swallowed():
    failures = [Failure(title="Junior QA", url="https://www.profesia.sk/praca/x/O5000010",
                        error="daily quota exhausted")]

    text = render_markdown(report(failures=failures), min_score=40)

    assert "Junior QA" in text
    assert "daily quota exhausted" in text


def test_a_day_with_nothing_new_still_produces_a_readable_report():
    text = render_markdown(report(seen=41), min_score=40)

    assert "2026-09-10" in text
    assert text.strip(), "an empty day must still say something"


def test_the_russian_report_uses_the_russian_reasons(two_scored):
    text = render_markdown(report(judged=two_scored), min_score=40, language="ru")

    assert "стек совпадает" in text
    assert "stack matches" not in text


def test_the_english_report_uses_the_english_reasons(two_scored):
    text = render_markdown(report(judged=two_scored), min_score=40, language="en")

    assert "stack matches" in text
    assert "стек совпадает" not in text


def test_the_russian_report_is_russian_throughout(two_scored):
    """A report half in English reads worse than one consistently in either."""
    rejected = [Rejected(card=card("O5000009", "Senior Architect"), reason="deal-breaker")]
    text = render_markdown(report(judged=two_scored, rejected=rejected), min_score=40, language="ru")

    assert "Open the posting" not in text
    assert "Filtered out" not in text
    assert "Вакансии" in text


def test_russian_falls_back_to_english_when_the_model_gave_only_one_language():
    """Better a reason in the wrong language than a vacancy with no reasons."""
    only_english = [Judged(card=card(), score=80, reasons=["stack matches"], reasons_ru=[], stage="final")]

    text = render_markdown(report(judged=only_english), min_score=40, language="ru")

    assert "stack matches" in text


def test_an_unknown_language_is_refused_rather_than_silently_english():
    with pytest.raises(ValueError):
        render_markdown(report(), min_score=40, language="sk")


def test_the_summary_leads_with_the_date_and_the_counts(two_scored):
    text = render_telegram_summary(report(seen=99, judged=two_scored), min_score=40)

    assert text.startswith("<b>Vacancies — 2026-09-10</b>")
    assert "Seen 99" in text


def test_the_summary_lists_only_strong_matches_best_first(two_scored):
    text = render_telegram_summary(report(judged=list(reversed(two_scored))), min_score=40)
    assert text.index("Junior Data Engineer") < text.index("Student Data Analyst")

    only_top = render_telegram_summary(report(judged=two_scored), min_score=60)
    assert "Student Data Analyst" not in only_top


def test_a_summary_entry_links_the_title_and_shows_the_card_facts(two_scored):
    text = render_telegram_summary(report(judged=two_scored), min_score=40)

    assert '<b>87</b> · <a href="https://www.profesia.sk/praca/acme/O5000001">Junior Data Engineer</a>' in text
    assert "ACME · home office · 1 300 - 1 600 EUR/mesiac" in text


def test_the_summary_leaves_the_reasons_to_the_file(two_scored):
    text = render_telegram_summary(report(judged=two_scored), min_score=40)

    assert "stack matches" not in text


def test_markup_in_a_title_cannot_break_the_message():
    judged = [Judged(card=card(title="C++ & <Rust> Developer"), score=80, stage="final")]

    text = render_telegram_summary(report(judged=judged), min_score=40)

    assert "C++ &amp; &lt;Rust&gt; Developer" in text
    assert "<Rust>" not in text


def test_a_day_without_strong_matches_still_gets_a_message():
    """Silence from the bot has to mean something is broken, never 'nothing today'."""
    text = render_telegram_summary(report(seen=41), min_score=40)

    assert "Nothing scored above the threshold today." in text


def test_the_russian_summary_is_russian(two_scored):
    text = render_telegram_summary(report(judged=two_scored), min_score=40, language="ru")

    assert "Вакансии" in text
    assert "Seen" not in text


def test_a_long_day_is_cut_to_one_message_and_says_how_many_are_left():
    judged = [
        Judged(card=card(f"O{5000000 + number}", "Junior Data Engineer " + "x" * 150), score=90, stage="final")
        for number in range(60)
    ]

    text = render_telegram_summary(report(judged=judged), min_score=40)

    shown = text.count("<a href=")
    assert telegram_length(text) <= TELEGRAM_LIMIT
    assert 0 < shown < 60
    assert f"…and {60 - shown} more — in the file" in text


def test_a_day_that_fits_has_no_tail(two_scored):
    text = render_telegram_summary(report(judged=two_scored), min_score=40)

    assert "in the file" not in text


def test_message_length_is_counted_the_way_telegram_counts_it():
    assert telegram_length("я") == 1
    assert telegram_length("😀") == 2


def test_an_unknown_language_is_refused_for_the_summary_too():
    with pytest.raises(ValueError):
        render_telegram_summary(report(), min_score=40, language="sk")
