from pathlib import Path

import httpx
import pytest

from core.ports import VacancySource
from local_connectors.vacancy_source import ProfesiaSource

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "profesia_listing.html").read_text(encoding="utf-8")

LIST_URL = "https://www.profesia.sk/praca/bratislava/?count_days=1"


def source_with(handler, **kwargs) -> ProfesiaSource:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return ProfesiaSource(list_url=LIST_URL, client=client, throttle=0, **kwargs)


def test_the_source_satisfies_the_port_it_is_declared_against():
    """The protocol drifted out of sync with the implementation once already."""
    source = source_with(lambda request: httpx.Response(200, text=LISTING))

    assert isinstance(source, VacancySource)


def test_reads_cards_from_the_listing():
    source = source_with(lambda request: httpx.Response(200, text=LISTING))

    cards = source.fetch_cards()

    assert len(cards) == 20
    assert cards[0].offer_id.startswith("O")


def test_walks_pages_until_one_adds_nothing_new():
    """Profesia keeps serving the last page instead of an empty one, so
    'no new offer ids' is the only reliable stop signal."""
    requested: list[str] = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=LISTING)

    source = source_with(handler, stop_page=5)
    cards = source.fetch_cards()

    assert len(cards) == 20, "the same page served twice must not double the cards"
    assert len(requested) == 2, "one page beyond the first is enough to detect repetition"


def test_a_failing_page_does_not_lose_the_pages_already_read():
    pages = iter([httpx.Response(200, text=LISTING), httpx.Response(500, text="boom")])
    source = source_with(lambda request: next(pages), stop_page=3)

    cards = source.fetch_cards()

    assert len(cards) == 20


def test_detail_returns_the_posting_text():
    body = "<html><body><h1>Junior Developer</h1><p>Python and SQL required.</p></body></html>"
    source = source_with(lambda request: httpx.Response(200, text=body))

    text = source.fetch_detail("https://www.profesia.sk/praca/acme/O5000001")

    assert "Python and SQL required." in text


def test_fetch_vacancies_still_serves_the_notebook():
    """notebooks/agent_sees.ipynb calls the old API; keep it working until the
    notebook is retired."""
    def handler(request):
        if "/praca/bratislava/" in str(request.url):
            return httpx.Response(200, text=LISTING)
        return httpx.Response(200, text="<html><body><h1>Role</h1><p>Body text.</p></body></html>")

    source = ProfesiaSource(list_url=LIST_URL, client=httpx.Client(transport=httpx.MockTransport(handler)),
                            throttle=0, use_cache=False)

    postings = source.fetch_vacancies(limit=2)

    assert len(postings) == 2
    assert set(postings[0]) == {"url", "title", "text"}
    assert "Body text." in postings[0]["text"]


def test_detail_refuses_a_host_outside_profesia():
    source = source_with(lambda request: httpx.Response(200, text="<html></html>"))

    with pytest.raises(ValueError):
        source.fetch_detail("https://evil.example.com/praca/acme/O5000001")
