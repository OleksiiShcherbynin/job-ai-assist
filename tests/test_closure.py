from pathlib import Path

from core.closure import is_closed, posting_state

FIXTURES = Path(__file__).parent / "fixtures"
CLOSED = (FIXTURES / "profesia_posting_closed.html").read_text(encoding="utf-8")
OPEN = (FIXTURES / "profesia_posting_open.html").read_text(encoding="utf-8")


def page(body: str) -> str:
    return f"<html><body><h1>Junior Dev</h1><div>{body}</div></body></html>"


def test_the_closed_fixture_is_closed():
    assert is_closed(CLOSED) is True


def test_the_open_fixture_is_open():
    assert is_closed(OPEN) is False


def test_the_notice_split_across_whitespace_still_counts():
    assert is_closed(page("spoločnosť ukončila\n    zverejnenie  ponuky na stránke")) is True


def test_a_similar_phrase_without_diacritics_does_not_count():
    # Only Profesia's own notice closes a posting; a lookalike in a description must not.
    assert is_closed(page("ukoncila zverejnenie ponuky")) is False


def test_an_error_page_is_unknown_not_closed():
    assert posting_state(500, CLOSED) == "unknown"


def test_a_page_without_a_title_is_unknown():
    # A captcha or maintenance page answers 200 too; it says nothing about the posting.
    assert posting_state(200, "<html><body>Please verify you are human</body></html>") == "unknown"


def test_a_titled_page_without_notice_or_apply_button_is_unknown():
    # Found in final review: a Cloudflare challenge has an <h1> too ("Just a moment...").
    # Calling it "open" would hide a day the check never really ran.
    assert posting_state(200, "<html><body><h1>Just a moment...</h1></body></html>") == "unknown"


def test_a_closed_posting_is_closed():
    assert posting_state(200, CLOSED) == "closed"


def test_an_open_posting_is_open():
    assert posting_state(200, OPEN) == "open"
