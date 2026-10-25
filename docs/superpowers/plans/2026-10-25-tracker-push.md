# Push scored vacancies to job-apply-tracker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After every run (and on every 15-minute check while anything is pending) job-ai-assist delivers each scored vacancy to job-apply-tracker's ingest API, retrying until accepted, without ever slowing or breaking the run or the Telegram report.

**Architecture:** Ports & adapters as in the rest of the repo: a `VacancyTracker` port in `core/ports.py`, an httpx adapter in `local_connectors/tracker.py`, an outbox table in the existing SQLite store, and the wiring/sync loop in `app/`. Payloads are built at send time from the `vacancies` table, so a re-score (rough → final) is sent with its newest score.

**Tech Stack:** Python 3.12, httpx (already pinned), SQLite, pytest with `httpx.MockTransport` (no network in tests).

**Spec:** job-apply-tracker repo, `docs/superpowers/specs/2026-10-24-stage2-push-api-design.md`, sections "A1. Ingest API" (the contract) and "Part B".

## Global Constraints

- Contract: `POST {TRACKER_API_URL}/api/v1/vacancies/`, header `Authorization: Token <key>`, body `{"vacancies": [...]}`, **≤ 200 items** per request; response `{"results": [{"external_id", "status": "created"|"updated"|"invalid", "id"?, "errors"?}]}` in request order.
- Item fields: `external_id` (offer id), `title`, `company` (may be empty → tracker uses "Unknown company"), `url`, `location`, `salary` (card's `salary_text`), `score` (0–100), `score_stage` (`rough`|`final`), `reasons_en`, `reasons_ru` (lists, blank lines dropped), `first_seen_at` (ISO 8601 with timezone).
- Only scored vacancies are queued: statuses `scored-rough`, `scored-final`, `repost-of-*`. `score_stage` for `repost-of-X` comes from X's status (`scored-final` → `final`, otherwise `rough`).
- Accepted (`created`/`updated`) → leave the outbox. `invalid` → logged at WARNING with the errors, leaves the outbox (not retried). Transport error / non-200 → everything stays queued.
- Config only from the environment: `TRACKER_API_URL`, `TRACKER_API_TOKEN`. Either missing → sender off, one INFO line. The token is never printed, logged or placed on a command line; `install_redaction(token)` covers logs. The user edits `.env` himself (repo rule: Claude does not read `.env`).
- A tracker problem never fails the run, never delays Telegram: Telegram delivery runs first, the push after it, each in its own try/except.
- compose: `daily-run` joins network `default` and a network declared as `name: job-net` (not external), so the container starts with or without the tracker.
- Repo rules: TDD, `py -3.12 -m pytest tests/ -q` stays green with no network; never `git add -A` (the user's notebook WIP and untracked `CLAUDE.md`/`.claude/` stay out); commit style = plain imperative sentence (e.g. "Retry a busy minute where the pacer can see it").

## Review Focus

1. Tracker unreachable for days (laptop off, container down) → queue keeps growing and drains in batches of 200 once it is back; nothing is lost or duplicated — Task 3.
2. A rough score later replaced by a final judgement → the vacancy is re-queued and the tracker receives the final score — Task 1.
3. Tracker answers 401 (wrong/rotated token) → items stay queued, one WARNING per check, the run and Telegram continue — Task 4.
4. First deployment → the whole scored backlog is queued exactly once, also across container restarts — Task 1.
5. A response with fewer results than items sent (malformed tracker answer) → unmatched items stay queued instead of being marked delivered — Task 3.

---

### Task 1: Outbox in the store

**Files:** Modify `app/store.py`; test `tests/test_store.py`.

**Interfaces (produces):**
- Table `tracker_outbox(offer_id TEXT PRIMARY KEY, queued_at TEXT NOT NULL)`.
- `Store.record(...)` queues the offer into `tracker_outbox` (upsert, refreshing `queued_at`) whenever `status` is `scored-rough`, `scored-final` or starts with `repost-of-`.
- `Store.tracker_pending(limit: int) -> list[sqlite3.Row]` — outbox rows joined with `vacancies` (offer_id, title, company, url, card_json, score, status, reasons_json, first_seen) plus the original's status for reposts (`original_status`, NULL otherwise), oldest `queued_at` first.
- `Store.tracker_done(offer_ids: list[str]) -> None` — removes them from the outbox.
- `Store.queue_tracker_backlog() -> int` — queues every already-scored vacancy once; guarded by `bot_state` key `tracker_backlog_queued`; returns how many were queued (0 on later calls).

- [ ] **Step 1: Failing tests:** `test_a_scored_vacancy_is_queued_for_the_tracker`; `test_a_rejected_vacancy_is_not_queued_for_the_tracker`; `test_a_final_judgement_requeues_a_rough_score` (rough recorded, `tracker_done`, final recorded → pending again, score = final); `test_a_repost_carries_its_originals_status`; `test_delivered_vacancies_leave_the_outbox`; `test_pending_comes_oldest_first_and_respects_the_limit`; `test_the_backlog_is_queued_once_even_after_reopening` (two Store instances on one file → second call returns 0).
- [ ] **Step 2:** `py -3.12 -m pytest tests/test_store.py -q` → FAIL. **Step 3:** implement. **Step 4:** full suite green.
- [ ] **Step 5:** Commit "Queue every scored vacancy for the tracker in the store".

### Task 2: Tracker client (port + adapter)

**Files:** Modify `core/ports.py`; create `local_connectors/tracker.py`, `tests/test_tracker_client.py`.

**Interfaces (produces):**
- `core.ports.VacancyTracker(Protocol)`: `push(vacancies: list[dict]) -> list[dict]` — returns the tracker's `results` list.
- `local_connectors.tracker.TrackerClient(base_url: str, token: str, client: httpx.Client | None = None)` implementing it; `close()`. Timeout 30 s. Raises `TrackerError(message, status=None)` on transport errors and on non-200, with the token redacted (`local_connectors.telegram.redact`) and the exception not chained.

- [ ] **Step 1: Failing tests** (MockTransport): `test_the_client_satisfies_the_port`; `test_a_push_posts_the_batch_with_the_token_header` (path `/api/v1/vacancies/`, header `Authorization: Token fake-token`, JSON body `{"vacancies": [...]}`); `test_a_trailing_slash_in_the_base_url_is_harmless`; `test_the_results_are_returned`; `test_a_refusal_is_an_error_with_its_status` (401 → `TrackerError.status == 401`); `test_a_network_failure_never_shows_the_token`.
- [ ] **Steps 2–4:** FAIL → implement → green.
- [ ] **Step 5:** Commit "Talk to the tracker's ingest API through a port".

### Task 3: Sync — payloads, batches, results

**Files:** Create `app/tracker_sync.py`, `tests/test_tracker_sync.py`.

**Interfaces (produces):**
- `to_payload(row) -> dict` — builds one contract item from a `tracker_pending` row (salary from `card_json["salary_text"]`, location from `card_json["location"]`, reasons from `reasons_json` en/ru with blank lines dropped, `first_seen_at` = `first_seen`, `score_stage` per Global Constraints).
- `sync(store, tracker, batch_size: int = 200) -> SyncResult` (`sent`, `accepted`, `invalid` counts). Loops: take `batch_size` pending rows → `tracker.push` → mark accepted and invalid as done (invalid logged with their errors) → next batch, until empty. Results are matched to items by position and checked against `external_id`; an item without a matching result stays queued. A `TrackerError` propagates (items stay queued).

- [ ] **Step 1: Failing tests** with a fake tracker (records batches, returns scripted results): `test_payload_has_the_contract_fields` (exact dict for a scored-final row); `test_a_repost_is_sent_with_its_originals_stage`; `test_blank_reasons_are_dropped`; `test_accepted_items_leave_the_queue`; `test_invalid_items_are_logged_and_not_retried`; `test_a_failed_push_keeps_everything_queued`; `test_a_large_backlog_goes_out_in_batches_of_200` (450 → 200, 200, 50); `test_a_short_answer_leaves_unmatched_items_queued`.
- [ ] **Steps 2–4:** FAIL → implement → green.
- [ ] **Step 5:** Commit "Send queued vacancies to the tracker in batches and keep what was refused".

### Task 4: Wiring in the daily loop, compose network

**Files:** Modify `app/main.py`, `compose.yaml`, `.env.example`; test `tests/test_main.py`.

**Interfaces:**
- `tracker_settings() -> tuple[str | None, str | None]` — `TRACKER_API_URL`, `TRACKER_API_TOKEN`, stripped, empty → None.
- `push_to_tracker(config, tracker) -> None` — opens a Store, `sync`, logs one INFO line with counts when anything was sent; any exception → one WARNING "could not reach the tracker; will retry on the next check (…)"; never raises.
- In `main()`: when both settings exist → `install_redaction(token)`, `TrackerClient`, `store.queue_tracker_backlog()` once at startup (logged), and `push_to_tracker` after Telegram `deliver` on every loop pass (also with `--once`). Otherwise one INFO line "TRACKER_API_URL/TRACKER_API_TOKEN not set; the tracker push is off".
- `compose.yaml`: `daily-run.networks: [default, jobnet]`; top-level `networks: {default: {}, jobnet: {name: job-net}}` with a comment why it is not external.
- `.env.example`: `TRACKER_API_URL=http://job-apply-tracker:8000` and an empty `TRACKER_API_TOKEN=` with a comment where it comes from.

- [ ] **Step 1: Failing tests:** `test_the_tracker_push_is_off_without_settings` (no client built, info line); `test_a_finished_run_is_pushed_to_the_tracker` (fake tracker receives the run's scored vacancies with `--once`); `test_a_tracker_failure_does_not_fail_the_run` (tracker raises → exit code 0, warning logged, Telegram delivery still attempted first); `test_the_token_never_reaches_the_log`.
- [ ] **Steps 2–4:** FAIL → implement → green.
- [ ] **Step 5:** Commit "Push to the tracker after every check without letting it hold up the run".

### Task 5: Go live with the user

- [ ] **Step 1:** Full suite + the repo's CI checks locally (`py -3.12 -m pytest tests/ -q`, ruff if configured).
- [ ] **Step 2:** Final independent review of the branch; fix Critical/Important with tests.
- [ ] **Step 3:** Push branch; user opens PR, CI green, merges.
- [ ] **Step 4:** User adds `TRACKER_API_URL=http://job-apply-tracker:8000` and his `TRACKER_API_TOKEN` to `.env` himself, then `docker compose up -d --build` (not `restart`: it does not re-read `env_file`).
- [ ] **Step 5:** Verify without spending model quota: container log shows the backlog push counts; tracker inbox "Unprocessed" / "Score below 40" now list the backlog; imported applications got scores (adopted/updated, no duplicates).
- [ ] **Step 6:** Update both project dossiers (job-ai-assist, dj-jobs-web).
