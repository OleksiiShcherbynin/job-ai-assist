import re
import time
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from core.logic import strip_accents
from core.models import VacancyCard

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_ALLOWED_HOSTS = ("profesia.sk", "www.profesia.sk")


def _is_allowed_host(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    return hostname in _ALLOWED_HOSTS


_SALARY_NUMBER = re.compile(r"\d[\d\s ]*(?:[.,]\d+)?")
_HOME_OFFICE_HINT = "pracu z domu"


def _to_float(token: str) -> float:
    return float(token.replace(" ", "").replace(" ", "").replace(",", "."))


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Split a Profesia pay label into (min, max, period).

    Profesia states pay on every card because Slovak law requires it, in one of
    a few shapes: '1 800 EUR/mesiac', '1 500 - 1 900 EUR/mesiac',
    'Od 1 550 EUR/mesiac', '7 EUR/hod.', 'Od 6,12 EUR/hod.'.

    max is None for an open-ended 'Od' (from) figure. period is 'hour' or
    'month'; the two are never comparable as plain numbers, which is why a
    single min_salary threshold cannot be applied across both.
    """
    if not text or "EUR" not in text:
        return None, None, None

    lowered = text.lower()
    if "hod" in lowered:
        period = "hour"
    elif "mesiac" in lowered:
        period = "month"
    else:
        period = None

    numbers = [_to_float(m.group()) for m in _SALARY_NUMBER.finditer(text.split("EUR")[0])]
    if not numbers:
        return None, None, period

    if lowered.strip().startswith("od"):
        return numbers[0], None, period
    if len(numbers) >= 2:
        return numbers[0], numbers[1], period
    return numbers[0], numbers[0], period


def _text_of(row, selector: str) -> str | None:
    element = row.select_one(selector)
    return element.get_text(" ", strip=True) if element else None


def _salary_label(row) -> str | None:
    """The pay label, picked by content: a card carries other labels too."""
    for element in row.select('[class*="label"]'):
        text = element.get_text(" ", strip=True)
        if "EUR" in text:
            return text
    return None


def _canonical_url(url: str) -> str:
    """Drop search_id/rid tracking parameters: they change on every request and
    would make the same vacancy look new each day."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def parse_cards(html: str, base_url: str = "https://www.profesia.sk/") -> list[VacancyCard]:
    """Read search-listing rows without fetching any posting."""
    soup = BeautifulSoup(html, "html.parser")
    cards: list[VacancyCard] = []

    for row in soup.select("ul.list li.list-row"):
        anchor = row.select_one('h2 a[id^="offer"]')
        if anchor is None:
            continue

        url = _canonical_url(urljoin(base_url, anchor.get("href") or ""))
        offer = re.search(r"O\d+", url)
        if offer is None or not _is_allowed_host(url):
            continue

        location = _text_of(row, '[class*="job-location"]')
        salary_text = _salary_label(row)
        salary_min, salary_max, period = parse_salary(salary_text)

        cards.append(
            VacancyCard(
                offer_id=offer.group(),
                title=anchor.get_text(strip=True),
                url=url,
                company=_text_of(row, '[class*="employer"]'),
                location=location,
                salary_text=salary_text,
                salary_min=salary_min,
                salary_max=salary_max,
                salary_period=period,
                allows_home_office=_HOME_OFFICE_HINT in strip_accents((location or "").lower()),
                posted_label=_text_of(row, ".list-footer .info"),
            )
        )

    return cards


class ProfesiaSource:
    """Reads the search listing and, separately, individual postings.

    The split is what makes the daily run affordable: cards are cheap and carry
    enough to reject most vacancies, so a posting is only fetched once it has
    survived the card filter.
    """

    LIST_URL = "https://www.profesia.sk/praca/bratislava/?count_days=1&education_levels[]=2&education_levels[]=8&education_levels[]=3&education_levels[]=5&education_levels[]=9&jobtypes[]=1&jobtypes[]=2&jobtypes[]=4&jobtypes[]=32&offer_agent_flags=196&search_anywhere=student%2C+IT%2C+junior&sort_by=relevance"

    def __init__(
        self,
        list_url: str | None = None,
        start_page: int = 1,
        stop_page: int = 1,
        client: httpx.Client | None = None,
        throttle: float = 0.5,
        use_cache: bool = False,
    ) -> None:
        if start_page < 1 or stop_page < start_page:
            raise ValueError("expected 1 <= start_page <= stop_page")
        self.use_cache = use_cache
        self.list_url = list_url or self.LIST_URL
        self.start_page = start_page
        self.stop_page = stop_page
        self.throttle = throttle
        self._client = client or httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def fetch_cards(self) -> list[VacancyCard]:
        collected: dict[str, VacancyCard] = {}

        for page in range(self.start_page, self.stop_page + 1):
            url = self.list_url if page == self.start_page else f"{self.list_url}&page_num={page}"
            try:
                response = self._client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                break  # keep the pages already read rather than losing the run

            fresh = [c for c in parse_cards(response.text) if c.offer_id not in collected]
            if not fresh:
                # Past the last page Profesia repeats the final one instead of
                # serving an empty result, so "nothing new" is the stop signal.
                break

            for card in fresh:
                collected[card.offer_id] = card
            if self.throttle:
                time.sleep(self.throttle)

        return list(collected.values())

    def fetch_detail(self, url: str) -> str:
        if not _is_allowed_host(url):
            raise ValueError(f"refusing to fetch outside Profesia: {url}")

        response = self._client.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for junk in soup(["script", "style", "noscript"]):
            junk.decompose()
        body = soup.select_one(".job-ad__desc, .main-content, article, main, body")
        return body.get_text(separator=" ", strip=True) if body else ""

    def fetch_vacancies(self, limit: int = 15) -> list[dict]:
        """Cards and postings in one call, in the shape the notebook expects.

        Kept for notebooks/agent_sees.ipynb only; delete it with the notebook.
        The daily run uses fetch_cards/fetch_detail so it can reject vacancies
        before paying for the page load.
        """
        postings: list[dict] = []
        for index, card in enumerate(self.fetch_cards()[:limit]):
            postings.append({
                "url": card.url,
                "title": card.title,
                "text": self.fetch_detail(card.url),
            })
            if self.throttle and index < limit - 1:
                time.sleep(self.throttle)
        return postings
