# Closed Profesia postings reported to the tracker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Once a day, after the run and the tracker push, job-ai-assist asks job-apply-tracker which Profesia offers matter, fetches each posting page, and reports the closed ones back, without Gemini calls and without ever slowing or breaking the run or the Telegram report.

**Architecture:** Same ports & adapters as the Stage 2 push. The `VacancyTracker` port gains `offers_to_check` / `report_closed` (adapter: `local_connectors/tracker.py`); the `VacancySource` port gains a raw-page method `fetch_page` (adapter: `ProfesiaSource`); the closed/open decision is pure code in `core/closure.py`; the once-per-day guard and the pending closed-report live in the SQLite store; `app/closure_check.py` walks the list and `app/main.py` wires it in after the push.

**Tech Stack:** Python 3.12, httpx (already pinned), SQLite, pytest with `httpx.MockTransport` and fake ports (no network in tests).

**Spec:** job-apply-tracker repo, `docs/superpowers/specs/2026-10-26-auto-close-design.md`, sections "How closed is recognised", "Part B", "Testing (Part B)". Part A (the tracker side) is live.

## Global Constraints

- Contract, same token and header as the push (`Authorization: Token <key>`):
  - `GET {TRACKER_API_URL}/api/v1/vacancies/to-check/` → `{"vacancies": [{"external_id": "O5330945", "url": "https://www.profesia.sk/praca/builtmind/O5330945"}]}`.
  - `POST {TRACKER_API_URL}/api/v1/vacancies/closed/`, body `{"closed": [{"external_id", "seen_at"}]}`, **≤ 200 items**, `seen_at` ISO 8601 with offset → `{"results": [{"external_id", "status": "closed"|"unchanged"|"unknown"|"invalid"}]}`. Repeats are harmless (`unchanged`).
  - Throttle 60 requests/hour per user, shared with the push: the check makes at most 1 GET + ceil(n/200) POSTs a day.
- **closed ⇔ HTTP 200 and the page contains `ukončila zverejnenie ponuky`.** Anything else (fetch error, non-200, no `<h1>` on the page) is "don't know": skipped today, never reported.
- Only Profesia URLs (`www.profesia.sk`, bare `profesia.sk`) are fetched; others in the list are skipped without a request.
- About one request every **2 s** between page fetches; the pause is injectable and never slept in tests.
- Once per day, remembered in the store: the day is **not** marked when `GET to-check` fails; it **is** marked right after the pages are fetched, the closed ids stored as a pending report, and `POST closed` is retried on later checks from that list. Profesia is never walked twice in a day.
- Runs only when `TRACKER_API_URL`/`TRACKER_API_TOKEN` exist; after Telegram delivery and the tracker push, on every check and under `--once`; does not depend on the daily run succeeding.
- No Gemini calls. Any failure → one WARNING, never an exception out of the loop, never affects the run or Telegram.
- Fixtures: trimmed real Profesia postings (one closed, one open), tokens redacted (repo rule: never commit raw Profesia HTML — the pages embed `offer-criteria-api-token`/`region-api-token`, gitleaks is blocking).
- Repo rules: TDD, `py -3.12 -m pytest tests/ -q` green with no network; never `git add -A` (the user's notebook WIP, `CLAUDE.md`, `.claude/` stay out); commits are plain imperative sentences; the token is never printed, logged or put on a command line.

## Review Focus

1. Profesia serves a captcha / maintenance page with 200 (no `<h1>`, no notice) → every offer is "unknown" for today, none reported, day marked, checked again tomorrow — Task 1 (`posting_state`) and Task 5.
2. Tracker container not up yet when job-ai-assist boots → `GET to-check` fails, the day stays unmarked, the next 15-minute check tries again and walks Profesia once — Task 5.
3. `POST closed` fails after the walk (tracker restarted, throttle 429) → closed ids survive in the store, are re-sent on the next check without any Profesia request, and leave the store only once answered — Task 3, Task 5.
4. The to-check list carries a non-Profesia URL (hand-typed application) or a URL of another host named "profesia" (`profesia.sk.evil.com`) → no request is made for it — Task 5.
5. A tracker answer that is shorter than what was sent or names other ids → unmatched items stay pending; `invalid`/`unknown` items are dropped with one log line (retrying cannot fix them) — Task 5.

---

### Task 1: Pure detection — `core/closure.py` + fixtures

**Files:**
- Create: `core/closure.py`, `tests/test_closure.py`, `tests/fixtures/profesia_posting_closed.html`, `tests/fixtures/profesia_posting_open.html`.

**Interfaces (produces):**
- `CLOSED_NOTICE = "ukončila zverejnenie ponuky"`.
- `is_closed(html: str) -> bool` — True only when the notice is in the page text (whitespace inside the phrase collapsed, so a line break inside it still matches; HTML entities not expanded — the phrase has none).
- `posting_state(status: int, html: str) -> Literal["closed", "open", "unknown"]` — `unknown` when `status != 200` or the page has no `<h1` tag; `closed` when `is_closed(html)`; else `open`.

- [ ] **Step 1: Fixtures.** With a scratchpad script (not inline `-c`: Bash heredocs mangle backslashes), pick from `state/seen.db` two September offers' URLs and fetch each with the headers from `local_connectors/vacancy_source.py`; keep the first page whose text contains the notice and the first that does not and has a "Reagovať na ponuku" button. Trim each to `<html><body>` + the `<h1>` + the `div.alert.alert-warning` (closed) / the apply button (open) + a short description paragraph; replace any `*-api-token` value with `REDACTED`; add the same header comment as `profesia_listing.html` (date saved, what was trimmed and why). Check: `grep -c api-token tests/fixtures/profesia_posting_*.html` prints only redacted values.
- [ ] **Step 2: Failing tests** in `tests/test_closure.py`:
  - `test_the_closed_fixture_is_closed` — `is_closed(CLOSED) is True`.
  - `test_the_open_fixture_is_open` — `is_closed(OPEN) is False`.
  - `test_the_notice_split_across_whitespace_still_counts` — `"ukončila\n  zverejnenie ponuky"` inside an `<h1>` page → True.
  - `test_a_similar_phrase_without_diacritics_does_not_count` — `"ukoncila zverejnenie ponuky"` → False (only the exact notice).
  - `test_an_error_page_is_unknown_not_closed` — `posting_state(500, CLOSED) == "unknown"`.
  - `test_a_page_without_a_title_is_unknown` — `posting_state(200, "<html><body>Please verify you are human</body></html>") == "unknown"` (Review Focus #1).
  - `test_an_open_posting_is_open` / `test_a_closed_posting_is_closed` via `posting_state(200, …)`.
- [ ] **Step 3:** `py -3.12 -m pytest tests/test_closure.py -q` → FAIL (import error).
- [ ] **Step 4:** Implement `core/closure.py` (stdlib only: `re`; a module docstring saying why the exact notice and why no-`<h1>` is unknown).
- [ ] **Step 5:** `py -3.12 -m pytest tests/ -q` → green.
- [ ] **Step 6:** Commit "Recognise a closed Profesia posting from its page".

### Task 2: Raw page fetch on the source

**Files:**
- Modify: `core/ports.py` (`VacancySource`), `local_connectors/vacancy_source.py`; test `tests/test_profesia_source.py`.

**Interfaces (produces):**
- `VacancySource.fetch_page(url: str) -> tuple[int, str]` — status code and raw HTML; raises `ValueError` for a host this source does not own; does **not** raise on non-200 (transport errors propagate as the client's own exceptions).
- `ProfesiaSource.fetch_page` implementing it with the existing client and headers (no throttle inside: the caller paces).

- [ ] **Step 1: Failing tests:** `test_fetch_page_returns_status_and_raw_html` (200 + the HTML verbatim, `<h1>` tags still in it); `test_fetch_page_hands_back_a_non_200_instead_of_raising` (404 → `(404, "…")`); `test_fetch_page_refuses_a_host_outside_profesia` (`ValueError`, handler never called); the existing `test_the_source_satisfies_the_port…` keeps checking the protocol.
- [ ] **Steps 2–4:** FAIL → implement → `py -3.12 -m pytest tests/ -q` green.
- [ ] **Step 5:** Commit "Fetch a posting page raw so a caller can read its status".

### Task 3: Store — once-per-day marker and the pending closed report

**Files:**
- Modify: `app/store.py`; test `tests/test_store.py`.

**Interfaces (produces):**
- Table `closed_outbox(offer_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)`.
- `Store.closure_checked_on() -> date | None` — the last day the Profesia walk finished; `bot_state` key `closure_checked_on` (ISO date).
- `Store.finish_closure_check(day: date, closed: list[str], seen_at: datetime) -> None` — one commit: marks the day and upserts every id into `closed_outbox` (`ON CONFLICT DO NOTHING`: the first sighting's `seen_at` stands).
- `Store.closed_pending(limit: int) -> list[sqlite3.Row]` — `offer_id`, `seen_at`, ordered by `seen_at, offer_id`.
- `Store.closed_done(offer_ids: list[str]) -> None`.

- [ ] **Step 1: Failing tests:** `test_a_fresh_store_has_never_checked_closures`; `test_a_finished_closure_check_remembers_its_day_and_queues_the_closed_ids` (day read back, pending rows carry `seen_at` ISO); `test_a_closed_id_seen_again_keeps_its_first_seen_at`; `test_reported_closures_leave_the_queue`; `test_a_check_with_nothing_closed_still_marks_the_day`; `test_the_closure_marker_survives_reopening_the_database`.
- [ ] **Steps 2–4:** FAIL → implement → green.
- [ ] **Step 5:** Commit "Remember the day's closure check and what it found in the store".

### Task 4: Tracker port + adapter — to-check and closed

**Files:**
- Modify: `core/ports.py` (`VacancyTracker`), `local_connectors/tracker.py`; test `tests/test_tracker_client.py`.

**Interfaces (produces):**
- `VacancyTracker.offers_to_check() -> list[dict]` — the `vacancies` list (`external_id`, `url`).
- `VacancyTracker.report_closed(closed: list[dict]) -> list[dict]` — sends `{"closed": closed}`, returns `results`.
- `TrackerClient`: `TO_CHECK_PATH = "/api/v1/vacancies/to-check/"`, `CLOSED_PATH = "/api/v1/vacancies/closed/"`; one private `_call(method, path, json=None) -> dict` holding the token header, the `TrackerError` mapping (transport → `status=None`, non-200 → status, non-JSON 200) and the redaction, used by all three public methods. `push` behaviour and its tests unchanged.

- [ ] **Step 1: Failing tests:** `test_offers_to_check_gets_the_list_with_the_token_header` (GET, path, header, returns the list); `test_report_closed_posts_the_items` (POST, body `{"closed": [...]}`, returns results); `test_a_refused_to_check_is_an_error_with_its_status` (401); `test_a_garbled_to_check_answer_is_an_error`; `test_the_client_satisfies_the_port` still passes with the grown protocol.
- [ ] **Steps 2–4:** FAIL → implement (refactor `push` onto `_call`) → green.
- [ ] **Step 5:** Commit "Ask the tracker which offers to check and tell it which closed".

### Task 5: The daily check — `app/closure_check.py`

**Files:**
- Create: `app/closure_check.py`, `tests/test_closure_check.py`.

**Interfaces:**
- Consumes: Task 1 `posting_state`; Task 2 `VacancySource.fetch_page`; Task 3 store methods; Task 4 tracker methods; `TrackerError`.
- Produces:
  - `PAUSE_SECONDS = 2.0`, `BATCH_SIZE = 200` (reuse the tracker limit).
  - `@dataclass CheckResult: listed=0, fetched=0, closed=0, skipped=0, reported=0, settled=0` (`reported` = items sent, `settled` = items that left the queue).
  - `check_closures(store: Store, tracker: VacancyTracker, source: VacancySource, today: date, sleeper: Callable[[float], None] | None = None, now: Callable[[], datetime] | None = None) -> CheckResult` — `None` resolves to `time.sleep` / `datetime.now(timezone.utc)` at call time (a default bound at definition could not be patched in tests).
  - Order inside: (1) `report_pending(store, tracker, result)` — drains `closed_outbox` in batches; a result matched by `external_id` with status `closed|unchanged|unknown|invalid` leaves the queue (`unknown`/`invalid` logged at WARNING once with the status); unmatched stays; a short answer stops the loop (as `tracker_sync.sync`). (2) If `store.closure_checked_on() == today` → return. (3) `tracker.offers_to_check()`; a `TrackerError` propagates (day unmarked). (4) Walk: skip non-Profesia hosts (`urlparse(url).hostname in {"www.profesia.sk", "profesia.sk"}`); `sleeper(PAUSE_SECONDS)` before every fetch except the first; `fetch_page` inside `try/except Exception` → skipped; `posting_state` → closed ids collected, `unknown` counted as skipped. (5) `store.finish_closure_check(today, closed_ids, now())`. (6) `report_pending` again. A `TrackerError` from step 6 propagates after the store was updated.

- [ ] **Step 1: Failing tests** with `FakeTracker` (scripted `offers_to_check` list, records `report_closed` batches, `fail_get`/`fail_post` flags), `FakeSource` (dict url → `(status, html)` or an exception to raise; records the order of fetched urls) and `sleeps: list[float]` as the sleeper:
  - `test_closed_postings_are_reported_with_seen_at` — 3 offers (closed fixture, open fixture, 404) → one POST `[{"external_id": "O1", "seen_at": NOW.isoformat()}]`, result counts `(listed, fetched, closed, skipped, reported) == (3, 3, 1, 1, 1)`, queue empty after.
  - `test_the_day_is_marked_only_after_the_walk` — `closure_checked_on()` is `today` after a successful call.
  - `test_a_failed_to_check_leaves_the_day_unmarked` (Review Focus #2) — `fail_get` → `TrackerError` raised, `closure_checked_on() is None`, no fetches.
  - `test_a_failed_report_is_retried_later_without_refetching` (Review Focus #3) — first call with `fail_post` → raises, day marked, pending holds `O1`; second call with a working tracker → POST from the store, `source.fetched == []` on the second call.
  - `test_a_second_call_the_same_day_does_nothing` — no GET, no fetches.
  - `test_non_profesia_urls_are_skipped_without_a_request` (Review Focus #4) — `https://jobs.example.com/x`, `https://profesia.sk.evil.com/O1` not fetched, counted skipped.
  - `test_fetches_are_paced` — 3 Profesia offers → `sleeps == [2.0, 2.0]`.
  - `test_a_fetch_error_is_skipped_not_closed` — source raises for one url → skipped, others still checked.
  - `test_a_page_without_a_title_is_skipped` (Review Focus #1) — captcha HTML → skipped.
  - `test_unknown_and_invalid_answers_leave_the_queue_with_a_warning` — statuses `unknown`, `invalid` → queue empty, `caplog` holds both ids.
  - `test_a_short_answer_keeps_unmatched_items_pending` (Review Focus #5).
  - `test_a_big_closed_list_goes_out_in_batches_of_200` — 250 pending → `[200, 50]`.
- [ ] **Steps 2–4:** FAIL → implement → green.
- [ ] **Step 5:** Commit "Check the tracker's offers on Profesia once a day and report the closed ones".

### Task 6: Wiring in the daily loop

**Files:**
- Modify: `app/main.py`; test `tests/test_main.py`.

**Interfaces:**
- `check_closed_postings(config: RunConfig, tracker: VacancyTracker, today: date) -> None` — opens a `Store` and a `ProfesiaSource(list_url=config.list_url, start_page=config.start_page, stop_page=config.stop_page)`, calls `check_closures`, closes both; logs one INFO line when anything was fetched or reported: `checked %d postings on Profesia: %d closed, %d skipped; reported %d to the tracker`; `TrackerError` 401/403 → the existing "rejected the token" wording; any other exception → WARNING `could not check closed postings; will retry on the next check (%s)`. Never raises.
- In `main()`: right after `push_to_tracker(config, tracker)`, `check_closed_postings(config, tracker, today)` when `tracker is not None`. `ProfesiaSource` is looked up on the module so tests can replace it.

- [ ] **Step 1: Failing tests** (extend the file's `FakeTracker` with `offers_to_check`/`report_closed`; monkeypatch `main_module.ProfesiaSource` with a factory returning a `FakeSource` that serves the closed fixture; `monkeypatch.setattr("app.closure_check.time.sleep", lambda s: None)` so a list of several offers never sleeps):
  - `test_closed_postings_are_reported_after_the_push` — `--once`, tracker lists `O1` → `report_closed` received `O1`, INFO line present, exit 0.
  - `test_the_closure_check_runs_after_the_tracker_push` — order recorded: push before to-check.
  - `test_a_closure_check_failure_does_not_fail_the_run` — `offers_to_check` raises `TrackerError` → exit 0, "could not check closed postings" in log, day unmarked.
  - `test_the_closure_check_is_off_without_tracker_settings` — no `ProfesiaSource` built for the check (factory fails the test if called while `run_today` is stubbed).
  - `test_the_closure_check_does_not_need_todays_run` — `run_today` raises, tracker on → check still happened, exit 1 (run failed) but `report_closed` was called.
- [ ] **Steps 2–4:** FAIL → implement → `py -3.12 -m pytest tests/ -q` green.
- [ ] **Step 5:** Commit "Check for closed postings after every push without letting it hold up the run".

### Task 7: Go live with the user

- [ ] **Step 1:** Full suite green, `py -3.12 -m bandit -r core local_connectors -q` clean of HIGH; `grep -rc api-token tests/fixtures/` shows only `REDACTED` values.
- [ ] **Step 2:** Final independent review of the branch on a fable subagent; fix Critical/Important with tests.
- [ ] **Step 3:** Push `feature/auto-close`; the user opens the PR, CI green, merges.
- [ ] **Step 4:** User: `docker compose up -d --build` from main. Verify: `docker compose logs --since 30m daily-run | grep -i "closed postings\|checked"`; the tracker shows "Closed on Profesia" on the closed ones. Nothing here spends model quota.
- [ ] **Step 5:** Update both project dossiers (job-ai-assist `tracker.md`: NEXT → done, how the check works, recovery `DELETE FROM bot_state WHERE key='closure_checked_on'` to re-walk today; dj-jobs-web).
