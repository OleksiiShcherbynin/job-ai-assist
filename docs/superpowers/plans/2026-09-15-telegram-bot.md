# Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each daily run, a Telegram bot sends the user a summary and the full report file in the language they picked, and answers `/today` and `/report <date>`.

**Architecture:** A `Messenger` port with a thin `httpx` adapter (`TelegramClient`). `app/bot.py` holds all bot behaviour against that port and the SQLite `Store`. `app/main.py` queues a delivery after each finished run, delivers from the main loop, and runs a daemon long-polling thread for commands that can wake the loop.

**Tech Stack:** Python 3.12, stdlib `sqlite3`/`threading`/`logging`, `httpx` 0.28.1 (already a dependency), pytest 8.3.2 with `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-09-15-telegram-bot-design.md`

## Global Constraints

- Run tests with `py -3.12 -m pytest tests/ -q`. No test touches the network or a real bot.
- No new dependencies. No change to `config.toml`, `compose.yaml`, `Dockerfile`, `requirements*.txt`.
- Secrets come only from the environment: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Never in code, tests, commits or shell commands (the machine's audit hook logs every command). Tests use the string `fake-token`, which gitleaks' Telegram rule does not match.
- Never `git add -A` or `git add .`: `notebooks/agent_sees.ipynb` is the user's uncommitted work. Stage explicit paths.
- Messages use Telegram HTML parse mode. A message must fit 4096 characters, measured in UTF-16 code units.
- Callback data is exactly `lang:ru` / `lang:en`. Bot state keys are exactly `language` and `update_offset`.
- Code comments and docstrings in English, explaining *why*, matching the existing modules. Commit messages: one sentence-case line in the repo's style (no `feat:` prefix), then a blank line and `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File map

| File | Responsibility |
|---|---|
| `app/store.py` (modify) | `deliveries` and `bot_state` tables; `Delivery`; queue/lookup/state methods |
| `app/report.py` (modify) | `render_telegram_summary`, shared counts line |
| `core/ports.py` (modify) | `Messenger` protocol |
| `local_connectors/telegram.py` (create) | `TelegramClient`, `TelegramError`, `redact` |
| `app/bot.py` (create) | `Bot` (commands, delivery, listen loop), `parse_report_date`, bot texts |
| `app/main.py` (modify) | env settings, redacting log filter, queueing, delivery, listener thread, wake event |
| `.env.example` (modify) | the two new variables |
| `tests/test_store.py`, `tests/test_report.py`, `tests/test_main.py` (modify) | tests for the above |
| `tests/test_telegram_client.py`, `tests/test_bot.py` (create) | adapter and bot tests |

---

### Task 1: Delivery queue and bot state in the store

**Files:**
- Modify: `app/store.py` (schema string at lines 24-54, new dataclass after `Verdict`, new methods at the end of `Store`)
- Test: `tests/test_store.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `app.store.Delivery` — dataclass `Delivery(day: date, summaries: dict[str, str])`, keys `"en"` and `"ru"`.
  - `Store.queue_delivery(day: date, summaries: dict[str, str]) -> None`
  - `Store.pending_deliveries() -> list[Delivery]` (oldest first)
  - `Store.delivery(day: date) -> Delivery | None` (sent or not)
  - `Store.mark_delivered(day: date) -> None`
  - `Store.get_state(key: str) -> str | None`
  - `Store.set_state(key: str, value: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py` (it already imports `date`, `pytest`, `Store` and defines the `store` fixture; add `Delivery` to the `app.store` import line):

```python
from app.store import Delivery, Store, quota_day
```

```python
SUMMARIES = {"en": "<b>Vacancies</b>", "ru": "<b>Вакансии</b>"}


def test_a_queued_report_waits_to_be_sent(store):
    store.queue_delivery(date(2026, 9, 15), SUMMARIES)

    assert store.pending_deliveries() == [Delivery(day=date(2026, 9, 15), summaries=SUMMARIES)]


def test_queued_reports_go_out_oldest_first(store):
    """Each day's report holds that day's vacancies; a newer one does not replace it."""
    store.queue_delivery(date(2026, 9, 16), SUMMARIES)
    store.queue_delivery(date(2026, 9, 14), SUMMARIES)

    assert [item.day for item in store.pending_deliveries()] == [date(2026, 9, 14), date(2026, 9, 16)]


def test_a_delivered_report_is_no_longer_pending(store):
    store.queue_delivery(date(2026, 9, 15), SUMMARIES)

    store.mark_delivered(date(2026, 9, 15))

    assert store.pending_deliveries() == []


def test_rerunning_a_day_queues_the_corrected_summary_again(store):
    """A --force rerun rewrites the report; the user should get the new one."""
    store.queue_delivery(date(2026, 9, 15), SUMMARIES)
    store.mark_delivered(date(2026, 9, 15))

    store.queue_delivery(date(2026, 9, 15), {"en": "fixed", "ru": "исправлено"})

    assert store.pending_deliveries() == [
        Delivery(day=date(2026, 9, 15), summaries={"en": "fixed", "ru": "исправлено"})
    ]


def test_a_sent_summary_can_still_be_looked_up(store):
    """/report must return the same summary the morning message had."""
    store.queue_delivery(date(2026, 9, 15), SUMMARIES)
    store.mark_delivered(date(2026, 9, 15))

    assert store.delivery(date(2026, 9, 15)) == Delivery(day=date(2026, 9, 15), summaries=SUMMARIES)
    assert store.delivery(date(2026, 9, 1)) is None


def test_bot_state_survives_reopening_the_database(tmp_path):
    path = tmp_path / "seen.db"
    Store(path).set_state("language", "ru")

    assert Store(path).get_state("language") == "ru"


def test_bot_state_can_be_overwritten(store):
    store.set_state("update_offset", "10")
    store.set_state("update_offset", "11")

    assert store.get_state("update_offset") == "11"


def test_unknown_bot_state_is_none(store):
    assert store.get_state("language") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_store.py -q`
Expected: collection error `ImportError: cannot import name 'Delivery' from 'app.store'`.

- [ ] **Step 3: Implement**

In `app/store.py`, append to the `_SCHEMA` string, before its closing `"""`:

```sql

CREATE TABLE IF NOT EXISTS deliveries (
    report_date TEXT PRIMARY KEY,
    summary_en  TEXT NOT NULL,
    summary_ru  TEXT NOT NULL,
    sent_at     TEXT
);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

After the `Verdict` dataclass add:

```python
@dataclass
class Delivery:
    """A day's Telegram summaries, in both languages.

    Stored rather than rebuilt from the Markdown: delivery can happen after a
    restart, when the run's report object is long gone, and /report has to show
    the same summary the morning message had.
    """

    day: date
    summaries: dict[str, str]
```

At the end of `class Store` add:

```python
    def queue_delivery(self, day: date, summaries: dict[str, str]) -> None:
        # A rerun of the same day replaces the summaries and clears sent_at, so
        # the corrected report goes out again instead of the stale one standing.
        self._connection.execute(
            "INSERT INTO deliveries (report_date, summary_en, summary_ru, sent_at) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(report_date) DO UPDATE SET summary_en = excluded.summary_en, "
            "summary_ru = excluded.summary_ru, sent_at = NULL",
            (day.isoformat(), summaries["en"], summaries["ru"]),
        )
        self._connection.commit()

    def pending_deliveries(self) -> list[Delivery]:
        rows = self._connection.execute(
            "SELECT report_date, summary_en, summary_ru FROM deliveries "
            "WHERE sent_at IS NULL ORDER BY report_date"
        ).fetchall()
        return [self._delivery(row) for row in rows]

    def delivery(self, day: date) -> Delivery | None:
        row = self._connection.execute(
            "SELECT report_date, summary_en, summary_ru FROM deliveries WHERE report_date = ?",
            (day.isoformat(),),
        ).fetchone()
        return self._delivery(row) if row else None

    def mark_delivered(self, day: date) -> None:
        self._connection.execute(
            "UPDATE deliveries SET sent_at = ? WHERE report_date = ?",
            (datetime.now(timezone.utc).isoformat(), day.isoformat()),
        )
        self._connection.commit()

    def get_state(self, key: str) -> str | None:
        row = self._connection.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._connection.commit()

    @staticmethod
    def _delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            day=date.fromisoformat(row["report_date"]),
            summaries={"en": row["summary_en"], "ru": row["summary_ru"]},
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_store.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/store.py tests/test_store.py
git commit -F - <<'EOF'
Queue each day's Telegram summaries in the state database

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

### Task 2: Telegram summary rendering

**Files:**
- Modify: `app/report.py`
- Test: `tests/test_report.py` (append)

**Interfaces:**
- Consumes: `RunReport`, `Judged`, `_PHRASES`, `_facts` already in `app/report.py`.
- Produces:
  - `app.report.TELEGRAM_LIMIT: int = 4096`
  - `app.report.telegram_length(text: str) -> int` — UTF-16 code units
  - `app.report.render_telegram_summary(report: RunReport, min_score: int, language: str = "en") -> str` — raises `ValueError` for an unknown language, like `render_markdown`

- [ ] **Step 1: Write the failing tests**

In `tests/test_report.py` change the import line to:

```python
from app.report import (TELEGRAM_LIMIT, Failure, Judged, Rejected, RunReport,
                        render_markdown, render_telegram_summary, telegram_length)
```

Append:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_report.py -q`
Expected: collection error `ImportError: cannot import name 'TELEGRAM_LIMIT'`.

- [ ] **Step 3: Implement**

In `app/report.py`:

1. Add `from html import escape` to the imports.
2. Below `LANGUAGES = ("en", "ru")` add:

```python
TELEGRAM_LIMIT = 4096
"""Telegram's cap on one message, counted in UTF-16 code units."""
```

3. Add a `"more"` phrase to each language in `_PHRASES`:
   - `"en"`: `"more": "…and {count} more — in the file",`
   - `"ru"`: `"more": "…и ещё {count} — в файле",`

4. Replace the first four lines of `render_markdown`'s body and its counts block with shared helpers. Add these functions above `render_markdown`:

```python
def _phrases_for(language: str) -> dict[str, str]:
    if language not in _PHRASES:
        raise ValueError(f"no phrasing for language {language!r}; known: {', '.join(LANGUAGES)}")
    return _PHRASES[language]


def _counts(report: RunReport, min_score: int, phrases: dict[str, str], strong: int) -> str:
    return phrases["counts"].format(
        seen=report.seen,
        scored=len(report.judged),
        min_score=min_score,
        strong=strong,
        rejected=len(report.rejected),
    )
```

Then in `render_markdown` replace

```python
    if language not in _PHRASES:
        raise ValueError(f"no phrasing for language {language!r}; known: {', '.join(LANGUAGES)}")
    phrases = _PHRASES[language]
```

with `phrases = _phrases_for(language)`, and replace the `phrases["counts"].format(...)` call inside `lines = [...]` with `_counts(report, min_score, phrases, len(strong)),`.

5. Append at the end of the module:

```python
def telegram_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def render_telegram_summary(report: RunReport, min_score: int, language: str = "en") -> str:
    """The message that arrives with the report file.

    Only the strong matches, without reasons: the phone shows what is worth
    opening, the attached file explains why. HTML rather than MarkdownV2,
    because escaping three characters is harder to get wrong than eighteen.
    """
    phrases = _phrases_for(language)
    strong = sorted(
        (item for item in report.judged if item.score >= min_score),
        key=lambda item: item.score,
        reverse=True,
    )

    text = (
        f"<b>{escape(phrases['title'])} — {report.day.isoformat()}</b>\n"
        + escape(_counts(report, min_score, phrases, len(strong)))
    )
    if not strong:
        return f"{text}\n\n{escape(phrases['nothing'])}"

    def tail(count: int) -> str:
        return "\n\n" + escape(phrases["more"].format(count=count))

    for shown, item in enumerate(strong):
        entry = (
            f'\n\n<b>{item.score}</b> · <a href="{escape(item.card.url)}">{escape(item.card.title)}</a>\n'
            + escape(_facts(item.card))
        )
        left_after = len(strong) - shown - 1
        needed = telegram_length(text + entry) + (telegram_length(tail(left_after)) if left_after else 0)
        if needed > TELEGRAM_LIMIT:
            # The previous step reserved room for exactly this tail.
            return text + tail(len(strong) - shown)
        text += entry
    return text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_report.py -q`
Expected: all pass, including the pre-existing `render_markdown` tests.

- [ ] **Step 5: Commit**

```bash
git add app/report.py tests/test_report.py
git commit -F - <<'EOF'
Render a one-message Telegram summary of the day's strong matches

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Messenger port and Telegram client

**Files:**
- Modify: `core/ports.py`
- Create: `local_connectors/telegram.py`
- Test: `tests/test_telegram_client.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `core.ports.Messenger` protocol:
    - `get_updates(offset: int | None, timeout: int) -> list[dict]`
    - `send_message(chat_id: int, html: str, buttons: list[tuple[str, str]] | None = None) -> None` — `buttons` are `(label, callback_data)`, one row
    - `send_document(chat_id: int, path: Path, caption: str | None = None) -> None`
    - `answer_callback(callback_id: str) -> None`
    - `edit_message(chat_id: int, message_id: int, html: str) -> None`
  - `local_connectors.telegram.TelegramClient(token: str, client: httpx.Client | None = None)` implementing `Messenger`, plus `close() -> None`
  - `local_connectors.telegram.TelegramError(message: str, status: int | None = None, retry_after: int | None = None)` with attributes `status`, `retry_after`
  - `local_connectors.telegram.redact(text: str, secret: str | None) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_telegram_client.py`:

```python
import json

import httpx
import pytest

from core.ports import Messenger
from local_connectors.telegram import TelegramClient, TelegramError, redact

TOKEN = "fake-token"


def client_with(handler) -> TelegramClient:
    return TelegramClient(TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))


def ok(result=True) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def recording(result=True):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return ok(result)

    return seen, handler


def test_the_client_satisfies_the_port():
    assert isinstance(client_with(lambda request: ok()), Messenger)


def test_a_message_is_sent_as_html_to_the_method_url():
    seen, handler = recording()

    client_with(handler).send_message(42, "<b>hi</b>")

    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(seen[0].content)
    assert (body["chat_id"], body["text"], body["parse_mode"]) == (42, "<b>hi</b>", "HTML")
    assert "reply_markup" not in body


def test_buttons_become_one_row_of_an_inline_keyboard():
    seen, handler = recording()

    client_with(handler).send_message(42, "pick", buttons=[("Русский", "lang:ru"), ("English", "lang:en")])

    assert json.loads(seen[0].content)["reply_markup"] == {"inline_keyboard": [[
        {"text": "Русский", "callback_data": "lang:ru"},
        {"text": "English", "callback_data": "lang:en"},
    ]]}


def test_updates_come_back_and_the_offset_is_passed_on():
    seen, handler = recording(result=[{"update_id": 7}])

    updates = client_with(handler).get_updates(offset=7, timeout=50)

    assert updates == [{"update_id": 7}]
    body = json.loads(seen[0].content)
    assert (body["offset"], body["timeout"]) == (7, 50)


def test_a_document_is_uploaded_under_its_own_name(tmp_path):
    path = tmp_path / "2026-09-15.ru.md"
    path.write_text("# Вакансии", encoding="utf-8")
    seen, handler = recording()

    client_with(handler).send_document(42, path)

    assert seen[0].url.path.endswith("/sendDocument")
    assert b'filename="2026-09-15.ru.md"' in seen[0].content
    assert "# Вакансии".encode("utf-8") in seen[0].content


def test_a_refusal_carries_the_status_and_retry_after():
    def handler(request):
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests: retry after 3",
                                         "parameters": {"retry_after": 3}})

    with pytest.raises(TelegramError) as caught:
        client_with(handler).answer_callback("q1")

    assert (caught.value.status, caught.value.retry_after) == (429, 3)


def test_a_rejected_token_is_recognisable():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    with pytest.raises(TelegramError) as caught:
        client_with(handler).get_updates(offset=None, timeout=50)

    assert caught.value.status == 401


def test_a_network_error_never_carries_the_token():
    """The token is part of every URL, and httpx puts URLs into its errors."""
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(TelegramError) as caught:
        client_with(handler).send_message(42, "x")

    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__, "the original httpx error must not be chained"


def test_redact_hides_every_occurrence_and_tolerates_no_secret():
    assert redact("a fake-token b fake-token", "fake-token") == "a *** b ***"
    assert redact("text", None) == "text"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_telegram_client.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'local_connectors.telegram'`.

- [ ] **Step 3: Implement the port**

In `core/ports.py` add `from pathlib import Path` to the imports and append:

```python
@runtime_checkable
class Messenger(Protocol):
    """A chat the bot talks through. Every text is Telegram HTML.

    Updates are passed through as the Bot API's own dicts: the bot reads four
    fields of them, which does not earn a model of its own.
    """

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        """Waits up to `timeout` seconds for updates at or after `offset`."""
        ...

    def send_message(self, chat_id: int, html: str,
                     buttons: list[tuple[str, str]] | None = None) -> None:
        """`buttons` are (label, callback data) pairs shown as one row."""
        ...

    def send_document(self, chat_id: int, path: Path, caption: str | None = None) -> None:
        ...

    def answer_callback(self, callback_id: str) -> None:
        ...

    def edit_message(self, chat_id: int, message_id: int, html: str) -> None:
        ...
```

- [ ] **Step 4: Implement the client**

Create `local_connectors/telegram.py`:

```python
"""Telegram Bot API over plain httpx.

Five methods do not justify python-telegram-bot and its own event loop. The
part that needs care is the token: it sits inside every request URL, so no text
leaving this module may carry a URL or an exception that quotes one.
"""

from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.telegram.org"


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


class TelegramError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        """HTTP status, or None when the request never got an answer."""
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self._client = client or httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        # Telegram holds the request open for `timeout` seconds when there is
        # nothing to say; the read timeout has to outlast that.
        return self._call("getUpdates", timeout=timeout + 10, json=payload)

    def send_message(self, chat_id: int, html: str,
                     buttons: list[tuple[str, str]] | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            # Otherwise the first vacancy link unfolds into a large Profesia card.
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": label, "callback_data": data} for label, data in buttons
            ]]}
        self._call("sendMessage", json=payload)

    def send_document(self, chat_id: int, path: Path, caption: str | None = None) -> None:
        path = Path(path)
        data = {"chat_id": str(chat_id)}
        if caption:
            data |= {"caption": caption, "parse_mode": "HTML"}
        with path.open("rb") as handle:
            self._call("sendDocument", data=data, files={"document": (path.name, handle, "text/markdown")})

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", json={"callback_query_id": callback_id})

    def edit_message(self, chat_id: int, message_id: int, html: str) -> None:
        self._call("editMessageText",
                   json={"chat_id": chat_id, "message_id": message_id, "text": html, "parse_mode": "HTML"})

    def _call(self, method: str, timeout: float | None = None, **request: Any) -> Any:
        url = f"{API_URL}/bot{self._token}/{method}"
        try:
            response = self._client.post(
                url, timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout, **request
            )
        except httpx.HTTPError as error:
            # "from None": the chained httpx error would print the URL, token and all.
            raise TelegramError(redact(f"{method}: {type(error).__name__}: {error}", self._token)) from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = body.get("description") or response.reason_phrase
            raise TelegramError(
                redact(f"{method}: {response.status_code} {description}", self._token),
                status=response.status_code,
                retry_after=(body.get("parameters") or {}).get("retry_after"),
            )
        return body.get("result")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_telegram_client.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add core/ports.py local_connectors/telegram.py tests/test_telegram_client.py
git commit -F - <<'EOF'
Talk to the Telegram Bot API without letting the token out

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Bot — sending reports and draining the queue

**Files:**
- Create: `app/bot.py`
- Test: `tests/test_bot.py` (create)

**Interfaces:**
- Consumes: `Store.pending_deliveries/delivery/mark_delivered/get_state` and `Delivery` (Task 1); `app.report.LANGUAGES`; `Messenger` and `TelegramError` (Task 3).
- Produces:
  - `app.bot.LANGUAGE_KEY = "language"`, `app.bot.OFFSET_KEY = "update_offset"`
  - `app.bot.TEXTS: dict[str, dict[str, str]]` with keys `chosen, help, bad_date, not_yet, no_report, no_summary, file_gone`; `app.bot.NEED_LANGUAGE: str`
  - `app.bot.parse_report_date(text: str, today: date) -> date | None`
  - `app.bot.Bot(messenger: Messenger, store: Store, report_dir: Path, chat_id: int | None, wake: threading.Event | None = None, today: Callable[[], date] = date.today)`
  - `Bot.language() -> str | None`
  - `Bot.deliver_pending() -> None` — raises `TelegramError` on the first failure; that report and later ones stay queued
  - `Bot.send_report(day: date) -> None` — on-demand, never changes the queue
  - `Bot._say(html: str, buttons: list[tuple[str, str]] | None = None, chat_id: int | None = None) -> None` — used by Task 5

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bot.py`:

```python
import threading
from datetime import date

import pytest

from app.bot import NEED_LANGUAGE, TEXTS, Bot, parse_report_date
from app.store import Store
from local_connectors.telegram import TelegramError

CHAT = 1001
TODAY = date(2026, 9, 16)
DAY = date(2026, 9, 15)
SUMMARIES = {"en": "<b>Vacancies — 2026-09-15</b>", "ru": "<b>Вакансии — 2026-09-15</b>"}


class FakeMessenger:
    """Records what the bot sends. `polls` scripts get_updates: each item is a
    list of updates or an exception to raise; once exhausted it sets `stop`."""

    def __init__(self, polls=()):
        self.sent: list[tuple] = []
        self.fail_on: set[str] = set()
        self.polls = list(polls)
        self.offsets: list[int | None] = []
        self.stop = threading.Event()

    def _record(self, kind, *details):
        if kind in self.fail_on:
            raise TelegramError(f"{kind} failed", status=500)
        self.sent.append((kind, *details))

    def get_updates(self, offset, timeout):
        self.offsets.append(offset)
        if not self.polls:
            self.stop.set()
            return []
        outcome = self.polls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def send_message(self, chat_id, html, buttons=None):
        self._record("message", chat_id, html, buttons)

    def send_document(self, chat_id, path, caption=None):
        self._record("document", chat_id, path.name, caption)

    def answer_callback(self, callback_id):
        self._record("answer", callback_id)

    def edit_message(self, chat_id, message_id, html):
        self._record("edit", chat_id, message_id, html)

    def texts(self):
        return [item[2] for item in self.sent if item[0] == "message"]


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "seen.db")
    yield opened
    opened.close()


@pytest.fixture
def reports(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    return folder


@pytest.fixture
def messenger():
    return FakeMessenger()


def make_bot(messenger, store, reports, chat_id=CHAT, wake=None) -> Bot:
    return Bot(messenger, store, reports, chat_id, wake=wake, today=lambda: TODAY)


def write_report(reports, day, language):
    path = reports / f"{day.isoformat()}.{language}.md"
    path.write_text("# report", encoding="utf-8")
    return path


# --- dates -----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2026-09-14", date(2026, 9, 14)),
    ("14.09", date(2026, 9, 14)),
    ("14.09.2026", date(2026, 9, 14)),
    ("4.9.2025", date(2025, 9, 4)),
    ("  14.09  ", date(2026, 9, 14)),
])
def test_report_dates_are_read_in_the_accepted_formats(text, expected):
    assert parse_report_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["", "yesterday", "31.02", "2026-9-14", "../../etc/passwd", "14/09"])
def test_anything_else_is_not_a_date(text):
    assert parse_report_date(text, TODAY) is None


# --- the queue -------------------------------------------------------------

def test_a_queued_report_goes_out_as_summary_then_file(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.sent == [
        ("message", CHAT, SUMMARIES["ru"], None),
        ("document", CHAT, "2026-09-15.ru.md", None),
    ]
    assert store.pending_deliveries() == []


def test_nothing_is_sent_before_a_language_is_chosen(messenger, store, reports):
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.sent == []
    assert len(store.pending_deliveries()) == 1


def test_nothing_is_sent_without_a_chat_id(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)

    make_bot(messenger, store, reports, chat_id=None).deliver_pending()

    assert messenger.sent == []
    assert len(store.pending_deliveries()) == 1


def test_a_failed_send_keeps_the_report_queued(messenger, store, reports):
    store.set_state("language", "en")
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "en")
    messenger.fail_on.add("document")

    with pytest.raises(TelegramError):
        make_bot(messenger, store, reports).deliver_pending()

    assert len(store.pending_deliveries()) == 1


def test_queued_reports_go_out_oldest_first(messenger, store, reports):
    store.set_state("language", "en")
    store.queue_delivery(date(2026, 9, 15), {"en": "fifteenth", "ru": "-"})
    store.queue_delivery(date(2026, 9, 14), {"en": "fourteenth", "ru": "-"})

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.texts()[0] == "fourteenth"
    assert messenger.texts().index("fifteenth") > 0


def test_a_missing_file_still_sends_the_summary_and_says_so(messenger, store, reports):
    store.set_state("language", "en")
    store.queue_delivery(DAY, SUMMARIES)

    make_bot(messenger, store, reports).deliver_pending()

    assert messenger.texts() == [SUMMARIES["en"], TEXTS["en"]["file_gone"]]
    assert store.pending_deliveries() == []


# --- on demand -------------------------------------------------------------

def test_a_requested_day_gets_its_summary_and_file(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)
    store.mark_delivered(DAY)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).send_report(DAY)

    assert messenger.sent == [
        ("message", CHAT, SUMMARIES["ru"], None),
        ("document", CHAT, "2026-09-15.ru.md", None),
    ]


def test_on_demand_sending_leaves_the_queue_alone(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(DAY, SUMMARIES)
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).send_report(DAY)

    assert len(store.pending_deliveries()) == 1


def test_a_day_from_before_the_bot_gets_the_file_with_a_note(messenger, store, reports):
    store.set_state("language", "ru")
    write_report(reports, date(2026, 9, 14), "ru")

    make_bot(messenger, store, reports).send_report(date(2026, 9, 14))

    assert messenger.sent == [("document", CHAT, "2026-09-14.ru.md", TEXTS["ru"]["no_summary"])]


def test_a_day_without_a_report_says_so(messenger, store, reports):
    store.set_state("language", "ru")

    make_bot(messenger, store, reports).send_report(date(2026, 9, 1))

    assert messenger.texts() == ["Отчёта за 01.09.2026 нет."]


def test_today_before_the_run_says_the_run_has_not_happened(messenger, store, reports):
    store.set_state("language", "en")

    make_bot(messenger, store, reports).send_report(TODAY)

    assert messenger.texts() == [TEXTS["en"]["not_yet"]]


def test_a_request_before_choosing_a_language_asks_for_one(messenger, store, reports):
    make_bot(messenger, store, reports).send_report(DAY)

    assert messenger.texts() == [NEED_LANGUAGE]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_bot.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.bot'`.

- [ ] **Step 3: Implement**

Create `app/bot.py`:

```python
"""The Telegram side of the daily run.

Everything the bot does is here, against the Messenger port and the Store, so
it can be tested without a network. Two threads use it, each with its own Bot
and its own Store: the main loop only drains the delivery queue, the listener
only answers commands. The queue therefore has a single sender, and a morning
report cannot go out twice.
"""

import logging
import re
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

from app.report import LANGUAGES
from app.store import Delivery, Store
from core.ports import Messenger

log = logging.getLogger("telegram")

LANGUAGE_KEY = "language"
OFFSET_KEY = "update_offset"

NEED_LANGUAGE = "Сначала выберите язык: /start\nChoose a language first: /start"

TEXTS = {
    "ru": {
        "chosen": "Язык: русский. Отчёт будет приходить сам после ежедневного прогона.",
        "help": "/today — отчёт за сегодня\n/report 14.09 — отчёт за дату\n/start — сменить язык",
        "bad_date": "Не понял дату. Примеры: /report 14.09, /report 14.09.2026, /report 2026-09-14",
        "not_yet": "Сегодняшнего прогона ещё не было.",
        "no_report": "Отчёта за {day} нет.",
        "no_summary": "Выжимки за этот день нет: отчёт появился раньше бота.",
        "file_gone": "Файл отчёта не найден в reports/.",
    },
    "en": {
        "chosen": "Language: English. The report will arrive by itself after the daily run.",
        "help": "/today — today's report\n/report 14.09 — report for a date\n/start — change language",
        "bad_date": "Could not read the date. Examples: /report 14.09, /report 14.09.2026, /report 2026-09-14",
        "not_yet": "Today's run has not happened yet.",
        "no_report": "No report for {day}.",
        "no_summary": "No summary for this day: the report predates the bot.",
        "file_gone": "The report file is missing from reports/.",
    },
}


def parse_report_date(text: str, today: date) -> date | None:
    """A date typed after /report, or None.

    Strict patterns only: the result names a file on disk, so anything that is
    not plainly a date is refused rather than guessed at.
    """
    text = text.strip()
    try:
        if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text):
            year, month, day = map(int, match.groups())
            return date(year, month, day)
        if match := re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text):
            day, month, year = match.groups()
            return date(int(year) if year else today.year, int(month), int(day))
    except ValueError:  # 31.02 and the like
        return None
    return None


class Bot:
    def __init__(
        self,
        messenger: Messenger,
        store: Store,
        report_dir: Path,
        chat_id: int | None,
        wake: threading.Event | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.messenger = messenger
        self.store = store
        self.report_dir = Path(report_dir)
        self.chat_id = chat_id
        self.wake = wake
        self.today = today

    def language(self) -> str | None:
        value = self.store.get_state(LANGUAGE_KEY)
        return value if value in LANGUAGES else None

    def deliver_pending(self) -> None:
        """Sends every queued report, oldest first.

        Stops at the first failure and lets it propagate: that report and the
        ones after it stay queued for the next attempt.
        """
        language = self.language()
        if self.chat_id is None or language is None:
            return
        for delivery in self.store.pending_deliveries():
            self._send(delivery.day, language, delivery)
            self.store.mark_delivered(delivery.day)

    def send_report(self, day: date) -> None:
        """A report on request. Never touches the queue."""
        language = self.language()
        if language is None:
            self._say(NEED_LANGUAGE)
            return

        delivery = self.store.delivery(day)
        if delivery is None and not self._report_path(day, language).is_file():
            texts = TEXTS[language]
            if day == self.today():
                self._say(texts["not_yet"])
            else:
                self._say(texts["no_report"].format(day=day.strftime("%d.%m.%Y")))
            return
        self._send(day, language, delivery)

    def _send(self, day: date, language: str, delivery: Delivery | None) -> None:
        texts = TEXTS[language]
        path = self._report_path(day, language)
        if delivery is not None:
            self._say(delivery.summaries[language])
        if path.is_file():
            # A report from before the bot has no stored summary; say why only the file came.
            self.messenger.send_document(self.chat_id, path, caption=None if delivery else texts["no_summary"])
        else:
            self._say(texts["file_gone"])

    def _report_path(self, day: date, language: str) -> Path:
        # Built from a date object, never from text a user typed.
        return self.report_dir / f"{day.isoformat()}.{language}.md"

    def _say(self, html: str, buttons: list[tuple[str, str]] | None = None, chat_id: int | None = None) -> None:
        self.messenger.send_message(self.chat_id if chat_id is None else chat_id, html, buttons)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_bot.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/bot.py tests/test_bot.py
git commit -F - <<'EOF'
Send queued and requested reports in the chosen language

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Bot — commands, language buttons, setup mode

**Files:**
- Modify: `app/bot.py`
- Test: `tests/test_bot.py` (append)

**Interfaces:**
- Consumes: everything `Bot` got in Task 4, `Store.set_state` (Task 1), `Messenger.answer_callback/edit_message` (Task 3).
- Produces:
  - `app.bot.CHOOSE_LANGUAGE: str`, `app.bot.LANGUAGE_BUTTONS: list[tuple[str, str]]`, `app.bot.SETUP: str` (format field `chat_id`)
  - `Bot.handle_update(update: dict) -> None`

- [ ] **Step 1: Write the failing tests**

In `tests/test_bot.py` change the `app.bot` import to:

```python
from app.bot import (CHOOSE_LANGUAGE, LANGUAGE_BUTTONS, NEED_LANGUAGE, SETUP, TEXTS, Bot,
                     parse_report_date)
```

Append:

```python
# --- commands and buttons --------------------------------------------------

def message(text, chat=CHAT, update_id=1):
    return {"update_id": update_id, "message": {"message_id": 5, "chat": {"id": chat}, "text": text}}


def button(data, chat=CHAT, update_id=2):
    return {"update_id": update_id,
            "callback_query": {"id": "q1", "data": data, "message": {"message_id": 9, "chat": {"id": chat}}}}


def test_start_offers_the_language_buttons(messenger, store, reports):
    make_bot(messenger, store, reports).handle_update(message("/start"))

    assert messenger.sent == [("message", CHAT, CHOOSE_LANGUAGE, LANGUAGE_BUTTONS)]
    assert LANGUAGE_BUTTONS == [("Русский", "lang:ru"), ("English", "lang:en")]


def test_a_command_addressed_by_bot_name_still_works(messenger, store, reports):
    make_bot(messenger, store, reports).handle_update(message("/start@JobAssistBot"))

    assert messenger.sent == [("message", CHAT, CHOOSE_LANGUAGE, LANGUAGE_BUTTONS)]


def test_a_stranger_gets_no_answer_and_changes_nothing(messenger, store, reports):
    bot = make_bot(messenger, store, reports)

    for update in (message("/start", chat=666), message("/today", chat=666), button("lang:ru", chat=666)):
        bot.handle_update(update)

    assert messenger.sent == []
    assert store.get_state("language") is None


def test_without_a_chat_id_start_tells_the_sender_their_own_id(messenger, store, reports):
    make_bot(messenger, store, reports, chat_id=None).handle_update(message("/start", chat=555))

    assert messenger.sent == [("message", 555, SETUP.format(chat_id=555), None)]
    assert "TELEGRAM_CHAT_ID=555" in SETUP.format(chat_id=555)


def test_without_a_chat_id_nothing_else_is_answered(messenger, store, reports):
    bot = make_bot(messenger, store, reports, chat_id=None)

    bot.handle_update(message("/today", chat=555))
    bot.handle_update(button("lang:ru", chat=555))

    assert messenger.sent == []


def test_pressing_a_language_saves_it_confirms_and_wakes_the_loop(messenger, store, reports):
    """Reports may already be queued; they should not wait for the next check."""
    wake = threading.Event()

    make_bot(messenger, store, reports, wake=wake).handle_update(button("lang:en"))

    assert store.get_state("language") == "en"
    assert messenger.sent == [("answer", "q1"), ("edit", CHAT, 9, TEXTS["en"]["chosen"])]
    assert wake.is_set()


def test_an_unknown_button_is_acknowledged_but_changes_nothing(messenger, store, reports):
    make_bot(messenger, store, reports).handle_update(button("lang:sk"))

    assert store.get_state("language") is None
    assert messenger.sent == [("answer", "q1")]


def test_today_sends_todays_report(messenger, store, reports):
    store.set_state("language", "ru")
    store.queue_delivery(TODAY, SUMMARIES)
    write_report(reports, TODAY, "ru")

    make_bot(messenger, store, reports).handle_update(message("/today"))

    assert messenger.sent[-1] == ("document", CHAT, "2026-09-16.ru.md", None)


def test_report_with_a_date_sends_that_day(messenger, store, reports):
    store.set_state("language", "ru")
    write_report(reports, DAY, "ru")

    make_bot(messenger, store, reports).handle_update(message("/report 15.09"))

    assert messenger.sent == [("document", CHAT, "2026-09-15.ru.md", TEXTS["ru"]["no_summary"])]


@pytest.mark.parametrize("text", ["/report вчера", "/report"])
def test_report_without_a_readable_date_explains_the_formats(messenger, store, reports, text):
    store.set_state("language", "ru")

    make_bot(messenger, store, reports).handle_update(message(text))

    assert messenger.texts() == [TEXTS["ru"]["bad_date"]]


def test_anything_else_gets_the_list_of_commands(messenger, store, reports):
    store.set_state("language", "en")

    make_bot(messenger, store, reports).handle_update(message("hello"))

    assert messenger.texts() == [TEXTS["en"]["help"]]


def test_a_message_without_text_gets_the_list_of_commands(messenger, store, reports):
    update = {"update_id": 1, "message": {"message_id": 5, "chat": {"id": CHAT}, "sticker": {}}}

    make_bot(messenger, store, reports).handle_update(update)

    assert messenger.texts() == [TEXTS["ru"]["help"]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_bot.py -q`
Expected: collection error `ImportError: cannot import name 'CHOOSE_LANGUAGE' from 'app.bot'`.

- [ ] **Step 3: Implement**

In `app/bot.py`, below `NEED_LANGUAGE` add:

```python
CHOOSE_LANGUAGE = "Язык отчёта / Report language"
LANGUAGE_BUTTONS = [("Русский", "lang:ru"), ("English", "lang:en")]

SETUP = (
    "Ваш chat id: <code>{chat_id}</code>\n"
    "Добавьте в .env строку TELEGRAM_CHAT_ID={chat_id} и пересоздайте контейнер: docker compose up -d\n\n"
    "Your chat id: <code>{chat_id}</code>\n"
    "Add TELEGRAM_CHAT_ID={chat_id} to .env and recreate the container: docker compose up -d"
)
```

Add these methods to `Bot`, after `language()`:

```python
    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_button(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def _on_message(self, message: dict) -> None:
        sender = (message.get("chat") or {}).get("id")
        command, _, argument = (message.get("text") or "").strip().partition(" ")
        # From a group or the command menu, commands arrive as /start@BotName.
        command = command.split("@", 1)[0]

        if self.chat_id is None:
            # Setup mode. The sender's own id is the only thing worth saying,
            # and it tells a stranger nothing they did not already have.
            if command == "/start" and sender is not None:
                self._say(SETUP.format(chat_id=sender), chat_id=sender)
            return
        if sender != self.chat_id:
            return

        texts = TEXTS[self.language() or "ru"]
        if command == "/start":
            self._say(CHOOSE_LANGUAGE, buttons=LANGUAGE_BUTTONS)
        elif command == "/today":
            self.send_report(self.today())
        elif command == "/report":
            day = parse_report_date(argument, self.today())
            if day is None:
                self._say(texts["bad_date"])
            else:
                self.send_report(day)
        else:
            self._say(texts["help"])

    def _on_button(self, query: dict) -> None:
        message = query.get("message") or {}
        if self.chat_id is None or (message.get("chat") or {}).get("id") != self.chat_id:
            return

        # Acknowledged first, whatever it was: otherwise the button keeps spinning.
        self.messenger.answer_callback(query["id"])
        data = query.get("data") or ""
        language = data.removeprefix("lang:")
        if not data.startswith("lang:") or language not in LANGUAGES:
            return

        self.store.set_state(LANGUAGE_KEY, language)
        self.messenger.edit_message(self.chat_id, message["message_id"], TEXTS[language]["chosen"])
        if self.wake is not None:
            # Reports may have been queued waiting for exactly this.
            self.wake.set()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_bot.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/bot.py tests/test_bot.py
git commit -F - <<'EOF'
Answer /start, /today and /report, and only in the owner's chat

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Bot — the long-polling loop

**Files:**
- Modify: `app/bot.py`
- Test: `tests/test_bot.py` (append)

**Interfaces:**
- Consumes: `Bot.handle_update` (Task 5), `Store.get_state/set_state` (Task 1), `TelegramError.status/retry_after` (Task 3), `FakeMessenger.polls/offsets/stop` in the test file (Task 4).
- Produces:
  - `app.bot.POLL_SECONDS = 50`, `FIRST_BACKOFF_SECONDS = 5`, `MAX_BACKOFF_SECONDS = 300`
  - `Bot.listen(stop: threading.Event, wait: Callable[[float], object] | None = None) -> None` — returns when `stop` is set or the token is rejected; `wait` defaults to `stop.wait`

- [ ] **Step 1: Write the failing tests**

Add `import logging` at the top of `tests/test_bot.py`, then append:

```python
# --- listening -------------------------------------------------------------

def listen(messenger, store, reports, **bot_options):
    waits: list[float] = []
    make_bot(messenger, store, reports, **bot_options).listen(messenger.stop, wait=waits.append)
    return waits


def test_updates_are_handled_and_the_offset_remembered(store, reports):
    messenger = FakeMessenger(polls=[[message("/start", update_id=10)]])

    listen(messenger, store, reports)

    assert ("message", CHAT, CHOOSE_LANGUAGE, LANGUAGE_BUTTONS) in messenger.sent
    assert store.get_state("update_offset") == "11"
    assert messenger.offsets == [None, 11]


def test_the_offset_survives_a_restart(store, reports):
    """Otherwise a restart re-reads an old /start and answers it again."""
    store.set_state("update_offset", "40")
    messenger = FakeMessenger()

    listen(messenger, store, reports)

    assert messenger.offsets == [40]


def test_a_rejected_token_stops_the_listener_with_one_clear_line(store, reports, caplog):
    messenger = FakeMessenger(polls=[TelegramError("getUpdates: 401 Unauthorized", status=401),
                                     [message("/start")]])

    listen(messenger, store, reports)

    assert messenger.sent == []
    assert len(messenger.polls) == 1, "nothing is polled after a 401"
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_an_outage_backs_off_doubling_up_to_five_minutes(store, reports):
    messenger = FakeMessenger(polls=[TelegramError("down")] * 9)

    waits = listen(messenger, store, reports)

    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 300]


def test_telegrams_retry_after_is_respected(store, reports):
    messenger = FakeMessenger(polls=[TelegramError("slow down", status=429, retry_after=30)])

    assert listen(messenger, store, reports) == [30]


def test_an_outage_is_logged_once_and_its_end_once(store, reports, caplog):
    """Nobody watches this log live; a line per retry would bury everything else."""
    caplog.set_level(logging.INFO, logger="telegram")
    messenger = FakeMessenger(polls=[TelegramError("down")] * 3 + [[]])

    listen(messenger, store, reports)

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "reachable again" in caplog.text


def test_an_update_that_breaks_the_handler_is_skipped_not_retried(store, reports):
    store.set_state("language", "ru")
    messenger = FakeMessenger(polls=[[message("/today", update_id=3)]])
    messenger.fail_on.add("message")

    listen(messenger, store, reports)

    assert store.get_state("update_offset") == "4"
    assert messenger.offsets == [None, 4]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_bot.py -q`
Expected: the new tests fail with `AttributeError: 'Bot' object has no attribute 'listen'`.

- [ ] **Step 3: Implement**

In `app/bot.py`:

1. Add `from local_connectors.telegram import TelegramError` to the imports.
2. Below `OFFSET_KEY` add:

```python
POLL_SECONDS = 50
"""How long Telegram may hold a getUpdates request open. Long polling answers
a button within a second while costing one idle request a minute."""

FIRST_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300
```

3. Add this method to `Bot`, after `handle_update`:

```python
    def listen(self, stop: threading.Event, wait: Callable[[float], object] | None = None) -> None:
        """Answers commands until `stop` is set or the token is rejected."""
        wait = wait or stop.wait
        delay = 0
        while not stop.is_set():
            stored = self.store.get_state(OFFSET_KEY)
            try:
                updates = self.messenger.get_updates(int(stored) if stored else None, POLL_SECONDS)
            except TelegramError as error:
                if error.status == 401:
                    log.error("Telegram rejected TELEGRAM_BOT_TOKEN — check .env. The bot stays off "
                              "until the container is recreated; the daily run is not affected.")
                    return
                if delay == 0:
                    log.warning("Telegram is unreachable; retrying quietly until it answers (%s)", error)
                delay = min(max(delay * 2, FIRST_BACKOFF_SECONDS), MAX_BACKOFF_SECONDS)
                wait(max(delay, error.retry_after or 0))
                continue

            if delay:
                log.info("Telegram is reachable again")
                delay = 0
            for update in updates:
                # Saved before handling: an update that keeps breaking the
                # handler must not be fetched again forever.
                self.store.set_state(OFFSET_KEY, str(update["update_id"] + 1))
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("could not answer a Telegram update")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_bot.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/bot.py tests/test_bot.py
git commit -F - <<'EOF'
Listen for commands with long polling that backs off quietly

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 7: Wire the bot into the daily loop

**Files:**
- Modify: `app/main.py`
- Modify: `.env.example`
- Test: `tests/test_main.py` (autouse fixture near the top, tests appended)

**Interfaces:**
- Consumes: `Store.last_run_date/queue_delivery` (Task 1), `render_telegram_summary` (Task 2), `TelegramClient`, `redact` (Task 3), `Bot.deliver_pending` (Task 4), `Bot.listen` (Task 6).
- Produces (all in `app.main`):
  - `telegram_settings() -> tuple[str | None, int | None]`
  - `RedactingFilter(logging.Filter)` and `install_redaction(secret: str) -> RedactingFilter`
  - `queue_report(config: RunConfig, report: RunReport) -> None`
  - `deliver(config: RunConfig, messenger: Messenger, chat_id: int | None) -> None` — never raises
  - `start_listener(token: str, chat_id: int | None, config: RunConfig, wake: threading.Event) -> threading.Thread`
  - module-level name `TelegramClient` (tests replace it)

- [ ] **Step 1: Write the failing tests**

In `tests/test_main.py`:

1. Add imports: `import logging` and `from app.main import RedactingFilter, install_redaction, telegram_settings`.
2. Below the imports add:

```python
@pytest.fixture(autouse=True)
def no_telegram_unless_asked(monkeypatch):
    """The developer's shell may carry real Telegram settings; tests never use them."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    yield
    for handler in logging.getLogger().handlers:
        for installed in [f for f in handler.filters if isinstance(f, RedactingFilter)]:
            handler.removeFilter(installed)
```

3. Append:

```python
class RecordingMessenger:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, chat_id, html, buttons=None):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append(("message", chat_id, html))

    def send_document(self, chat_id, path, caption=None):
        self.sent.append(("document", chat_id, path.name))


def with_telegram(monkeypatch, tmp_path, messenger, report, finished=True):
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1001")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "TelegramClient", lambda token: messenger)
    monkeypatch.setattr(main_module, "start_listener",
                        lambda *a: pytest.fail("--once must not start the listener"))

    def run(config, today):
        write_reports(report, config)
        if finished:
            store = Store(config.state_path)
            store.mark_run(report.day)
            store.close()
        return report

    monkeypatch.setattr(main_module, "run_today", run)
    store = Store(config_at(tmp_path).state_path)
    store.set_state("language", "en")
    store.close()
    return main_module


def test_a_finished_run_reaches_telegram_as_summary_and_file(tmp_path, monkeypatch, report):
    messenger = RecordingMessenger()
    main_module = with_telegram(monkeypatch, tmp_path, messenger, report)

    assert main_module.main(["--once"]) == 0

    assert messenger.sent[0][:2] == ("message", 1001)
    assert "Junior Data Engineer" in messenger.sent[0][2]
    assert messenger.sent[1] == ("document", 1001, "2026-09-10.en.md")


def test_an_unfinished_run_is_not_queued(tmp_path, monkeypatch, report):
    """A run cut short is not marked done and repeats every check; queueing it
    would send a message every fifteen minutes."""
    messenger = RecordingMessenger()
    main_module = with_telegram(monkeypatch, tmp_path, messenger, report, finished=False)

    main_module.main(["--once"])

    assert messenger.sent == []


def test_a_telegram_failure_does_not_fail_the_run(tmp_path, monkeypatch, report, caplog):
    messenger = RecordingMessenger(fail=True)
    main_module = with_telegram(monkeypatch, tmp_path, messenger, report)

    assert main_module.main(["--once"]) == 0
    assert "will retry" in caplog.text
    assert len(Store(config_at(tmp_path).state_path).pending_deliveries()) == 1


def test_without_a_token_the_bot_stays_off(tmp_path, monkeypatch, report):
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "TelegramClient", lambda token: pytest.fail("no token, no client"))
    monkeypatch.setattr(main_module, "run_today", lambda config, today: report)

    assert main_module.main(["--once"]) == 0
    assert Store(config_at(tmp_path).state_path).pending_deliveries() == []


def test_httpx_request_lines_are_kept_out_of_the_log(tmp_path, monkeypatch, report):
    """They carry the bot token in the URL, and long polling adds one a minute."""
    import app.main as main_module

    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "load_config", lambda path: config_at(tmp_path))
    monkeypatch.setattr(main_module, "run_today", lambda config, today: report)

    main_module.main(["--once"])

    assert logging.getLogger("httpx").level == logging.WARNING


def test_the_token_is_masked_in_every_log_line_and_traceback(caplog):
    install_redaction("fake-token")
    logger = logging.getLogger("anything")

    logger.error("GET https://api.telegram.org/bot%s/getUpdates", "fake-token")
    try:
        raise RuntimeError("failed at /botfake-token/sendMessage")
    except RuntimeError:
        logger.exception("boom")

    assert "fake-token" not in caplog.text
    assert "/bot***/getUpdates" in caplog.text


@pytest.mark.parametrize("raw_chat, expected", [("1001", 1001), ("-100200", -100200), ("", None), ("abc", None)])
def test_telegram_settings_come_from_the_environment(monkeypatch, raw_chat, expected):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " fake-token ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", raw_chat)

    assert telegram_settings() == ("fake-token", expected)


def test_an_empty_token_means_no_bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    assert telegram_settings() == (None, None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_main.py -q`
Expected: collection error `ImportError: cannot import name 'RedactingFilter' from 'app.main'`.

- [ ] **Step 3: Implement the helpers**

In `app/main.py`:

1. Add `import threading` to the stdlib imports. Add to the project imports:

```python
from app.bot import Bot
from app.report import LANGUAGES, RunReport, render_markdown, render_telegram_summary
from core.ports import Messenger
from local_connectors.telegram import TelegramClient, redact
```

(the existing `from app.report import ...` line is replaced by the one above).

2. Below `write_reports` add:

```python
def telegram_settings() -> tuple[str | None, int | None]:
    """Token and owner chat id. Both are secrets-adjacent, so only the
    environment carries them — never config.toml."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
    raw_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if re.fullmatch(r"-?\d+", raw_chat) else None
    return token, chat_id


class RedactingFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.secret)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.secret)
        return True


def install_redaction(secret: str) -> RedactingFilter:
    """Masks the secret in every line any handler writes, tracebacks included.

    The Telegram client already keeps the token out of its errors; this is the
    last line for whatever else might quote a URL. Handler filters rather than a
    logger filter, because records from child loggers skip the root's filters.
    """
    redacting = RedactingFilter(secret)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redacting)
    return redacting


def queue_report(config: RunConfig, report: RunReport) -> None:
    """Queues the day's summaries, but only for a run that finished.

    A run cut short by a refusing API is not marked done and repeats on every
    check; queueing it would send a message every fifteen minutes.
    """
    store = Store(config.state_path)
    try:
        if store.last_run_date() != report.day:
            return
        store.queue_delivery(report.day, {
            language: render_telegram_summary(report, config.min_score, language)
            for language in LANGUAGES
        })
    finally:
        store.close()


def deliver(config: RunConfig, messenger: Messenger, chat_id: int | None) -> None:
    """Drains the queue. A delivery problem never becomes a run problem."""
    store = Store(config.state_path)
    try:
        Bot(messenger, store, config.report_dir, chat_id).deliver_pending()
    except Exception as error:
        log.warning("could not send the report to Telegram; will retry on the next check (%s)", error)
    finally:
        store.close()


def start_listener(token: str, chat_id: int | None, config: RunConfig,
                   wake: threading.Event) -> threading.Thread:
    def listen() -> None:
        # Opened inside the thread: an sqlite3 connection may not cross threads.
        store = Store(config.state_path)
        client = TelegramClient(token)
        try:
            Bot(client, store, config.report_dir, chat_id, wake=wake).listen(threading.Event())
        except Exception:
            log.exception("the Telegram listener stopped")
        finally:
            client.close()
            store.close()

    thread = threading.Thread(target=listen, name="telegram", daemon=True)
    thread.start()
    return thread
```

- [ ] **Step 4: Wire them into `main()`**

Replace the body of `main()` from `logging.basicConfig(...)` to the end of the function with:

```python
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO. Telegram's URLs carry the bot token,
    # and long polling would add a line a minute; the other request lines were
    # noise already.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(override=False)

    if not os.environ.get("GOOGLE_API_KEY"):
        log.error("GOOGLE_API_KEY is not set; pass it with --env-file or the environment")
        return 2

    config = load_config(args.config)

    token, chat_id = telegram_settings()
    telegram: Messenger | None = None
    wake = threading.Event()
    if token:
        install_redaction(token)
        telegram = TelegramClient(token)
        if chat_id is None:
            log.info("TELEGRAM_CHAT_ID is not set: send /start to the bot to learn it; "
                     "reports are queued until then")
        if not args.once:
            start_listener(token, chat_id, config, wake)
    else:
        log.info("TELEGRAM_BOT_TOKEN is not set; the Telegram bot is off")

    failed = False

    while True:
        today = date.today()
        store = Store(config.state_path)
        done = already_ran_today(store, today)
        store.close()

        if done and not args.force:
            log.info("%s already done", today)
        else:
            log.info("running for %s", today)
            try:
                report = run_today(config, today)
                failed = False
                log.info("seen %d, scored %d, filtered %d, unprocessed %d",
                         report.seen, len(report.judged), len(report.rejected), len(report.failures))
            except Exception as error:
                failed = True
                if is_account_error(error):
                    # Diagnosed and actionable: say what to do, not where it broke.
                    # Nobody watches this log live, and a stack trace buries the
                    # one sentence that matters.
                    log.error(
                        "the Gemini API refuses every request for this key — check the "
                        "project's billing at https://ai.studio/projects. Nothing will "
                        "run until that is resolved. (%s)",
                        str(error)[:200],
                    )
                    return 3
                log.exception("the run failed; will try again on the next check")
            else:
                if telegram is not None:
                    try:
                        queue_report(config, report)
                    except Exception:
                        log.exception("could not queue the report for Telegram")

        if telegram is not None:
            deliver(config, telegram, chat_id)

        if args.once:
            # In the loop a failed run just waits for the next check. With --once
            # there is no next check inside this process, so a caller must be able
            # to tell a finished run from a broken one.
            return 1 if failed else 0
        # A chosen language wakes the loop early, so queued reports need not
        # wait out the rest of the interval.
        wake.wait(IDLE_CHECK_SECONDS)
        wake.clear()
```

- [ ] **Step 5: Document the variables**

Replace the contents of `.env.example` with:

```
GOOGLE_API_KEY=

# Optional. Without a token the Telegram bot is off.
TELEGRAM_BOT_TOKEN=
# Send /start to the bot and it replies with this number.
TELEGRAM_CHAT_ID=
```

- [ ] **Step 6: Run the whole suite**

Run: `py -3.12 -m pytest tests/ -q`
Expected: all pass (128 before this work plus the new tests).

- [ ] **Step 7: Commit**

```bash
git add app/main.py .env.example tests/test_main.py
git commit -F - <<'EOF'
Deliver each finished run to Telegram and listen for commands alongside

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 8: Live check and deployment

No code. Every step that needs the token is done by the user or by a script that reads `.env` itself; the token never appears in a command line.

- [ ] **Step 1: Secret scan of the branch**

Run: `git log -p main..HEAD | py -3.12 -c "import re,sys; hits=re.findall(r'\d{8,10}:AA[\w-]{30,}', sys.stdin.read()); print('token-shaped strings:', len(hits))"`
Expected: `token-shaped strings: 0`.

- [ ] **Step 2: User puts the token in `.env`**

Ask the user to add `TELEGRAM_BOT_TOKEN=<token>` to `.env` (a freshly revoked one, ideally). Do not Read `.env`.

- [ ] **Step 3: Rebuild the container**

Run: `docker compose up -d --build`
Today's run already happened, so the container logs `already done`, spends no Gemini quota, and queues nothing.
Then: `docker compose logs --since 5m` — expect `TELEGRAM_CHAT_ID is not set` and no traceback.

- [ ] **Step 4: User learns the chat id**

The user sends `/start`; the bot replies with the id. The user adds `TELEGRAM_CHAT_ID=<id>` to `.env`.
Run: `docker compose up -d` (recreates the container so `.env` is re-read).

- [ ] **Step 5: User walks the commands**

`/start` → buttons → pick a language → the message changes to the confirmation. Then `/report 15.09` → the 2026-09-15 file with the "no summary" caption. Then `/today` → the same file (today is 2026-09-15 if checked the same day). Then `hello` → the command list.

- [ ] **Step 6: One real summary, no quota spent**

Write `live_summary.py` in the session scratchpad:

```python
"""Sends one summary built from a hand-made report through the real bot."""
import os
from datetime import date

from dotenv import load_dotenv

from app.report import Judged, RunReport, render_telegram_summary
from core.models import VacancyCard
from local_connectors.telegram import TelegramClient

load_dotenv("D:/Cods/python_project/job-ai-assist/.env")
card = VacancyCard(offer_id="O1", title="Test <vacancy> & summary", company="ACME",
                   url="https://www.profesia.sk/", salary_text="1 500 EUR/mesiac", allows_home_office=True)
report = RunReport(day=date.today(), seen=1, judged=[Judged(card=card, score=90, stage="final")])
client = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"])
client.send_message(int(os.environ["TELEGRAM_CHAT_ID"]), render_telegram_summary(report, 40, "ru"))
print("sent")
```

Run from the repo root: `py -3.12 <scratchpad>/live_summary.py`
Expected: `sent`, and the user confirms the message looks right (bold score, clickable title, escaped `<vacancy> &`, no link preview).

- [ ] **Step 7: The token is not in the container log**

Run: `docker compose logs --since 30m | py -3.12 -c "import sys; t=sys.stdin.read(); print('api.telegram.org lines:', t.count('api.telegram.org'))"`
Expected: `api.telegram.org lines: 0`.

- [ ] **Step 8: Hand over**

Tomorrow's first check after wake runs, queues and sends the report by itself. Ask the user before pushing the branch and opening a PR (project agreement: features merge through GitHub PRs).

