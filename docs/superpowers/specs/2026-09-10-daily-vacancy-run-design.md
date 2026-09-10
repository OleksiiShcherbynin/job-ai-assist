# Daily vacancy run — design

Status: approved 2026-09-10. Supersedes the notebook-driven workflow for
scoring vacancies.

## Goal

Every morning, without the user opening a notebook, produce a Markdown report
of new Bratislava IT vacancies scored against the user's resume, plus a list of
what was filtered out and why.

## Decisions taken

| Question | Decision |
|---|---|
| Trigger | Self-contained container, `restart: unless-stopped`, "already ran today?" marker |
| Report format | Markdown, one file per day |
| Report content | New scored vacancies + collapsed list of rejects with reasons |
| Sources | Profesia.sk only |
| Filtering | Cheap card-level filter before any LLM call |

Rejected alternatives: Windows Task Scheduler (races Docker Desktop startup on
boot); HTML report; scraping Lenovo/Siemens/ING/Swiss Re (JS-rendered or
auth-walled, needs Playwright).

## Measured facts this design rests on

Gathered 2026-09-09 by throwaway probes against live Profesia and 20 reference
vacancies the user hand-picked as good matches.

- 81 vacancies/day with the current search string (`count_days=1`); 223+ over 7
  days. Broadening the query to `+data, analytik, brigada, trainee` gives 99/day.
- Listing cards already carry title, employer, location, remote hint and salary.
  Salary is present on 81/81 cards (Slovak law mandates disclosure).
- Stop-words in the title remove 13 of 81. Additionally requiring a positive
  keyword leaves 31 of 81, but visibly loses good roles ("IT konzultant",
  "IT technik") and admits junk ("Upratovač/ka v materskej škole").
- The current `deal_breakers` are all broken: `"Senior"` causes 2 false
  rejections out of 20 references (matching "pod vedením seniorného kolegu" and
  "nemusíš byť senior"); `"Midle"`, `"Middle"`, `"English C1"`, `"English C2"`
  match nothing at all. Real ads write "anglický jazyk B1/B2"; English C1/C2
  appears in 0 of 20.
- Matching stop-words against the title instead of the whole text drops false
  rejections from 5/20 to 1/20.
- Softec posted one vacancy under two offer ids (O5321098, O5350242) with an
  identical title — offer-id dedup alone will not catch reposts.
- No tech keyword appears in more than 35% of references, so `must_have` must
  stay empty.
- Salary mixes hourly (6–10 EUR/h) and monthly (1000–2000 EUR/mo) rates, so a
  single numeric `min_salary` threshold cannot work as written.

## Architecture

`core/` stays pure (models and rules, no I/O). `local_connectors/` holds
adapters. A new `app/` layer orchestrates.

```
app/
  main.py       entrypoint; "ran today?" guard; exit
  pipeline.py   step sequence
  store.py      SQLite: seen vacancies, quota counters, run marker
  report.py     Markdown rendering
  config.py     config.toml -> SearchPreferences, model roles, run settings
  pacing.py     client-side RPM/RPD limiter per model

core/
  models.py     + VacancyCard
  logic.py      + card_rejection_reason(); stop-words scoped to title

local_connectors/
  vacancy_source.py   fetch_vacancies() split into fetch_cards() / fetch_detail()
```

### Data flow

| Step | Network | LLM | Survivors |
|---|---|---|---|
| `fetch_cards` | 5 listing pages | — | 99 |
| `store.is_new` | — | — | new only |
| `card_rejection_reason` | — | — | ~83 |
| `fetch_detail` | 1 request each | — | ~83 |
| `extract` | — | 1 each, flash-lite | ~83 |
| `rejection_reason` | — | — | ~65 |
| rough `score_match` | — | 1 each, 3.5-flash-lite | ~65 |
| final judge | — | 1 each, 3.5-flash | ≤12 |

## State

SQLite at `state/seen.db` (stdlib `sqlite3`, no new dependency). Chosen over
JSON because JSON rewrites the whole file per write and loses everything if the
container dies mid-write.

Identity is the Profesia offer id (`O5355626`) parsed from the URL — not the
full URL, whose `search_id`/`rid` parameters change every request and would make
everything look new daily.

The positive-keyword requirement measured above is configurable and **off by
default**: losing "IT konzultant" costs more than letting a cleaning job through,
since a bad match simply scores low and sinks to the bottom of the report.

Secondary dedup on `(company, normalized title)` marks reposts; a repost inherits
the original's score and is never sent to `score_match` again.

Raw cards and detail text are stored, which makes "re-score everything under new
criteria without re-scraping" a query rather than a subsystem.

## Quota handling

Three limit types apply simultaneously: RPM, TPM and RPD. Daily quota resets at
midnight Pacific Time, not local midnight — around 09:00 in Bratislava, i.e.
after the morning run rather than before it.

Per-model limits live in `config.toml` because Google no longer publishes them.
The user's actual free-tier limits, read from AI Studio on 2026-09-10:

| Model | RPM | RPD |
|---|---|---|
| gemini-3.1-flash-lite | 15 | 500 |
| gemini-3.5-flash-lite | 15 | 500 |
| gemini-3.5-flash | 5 | **20** |

These are throughput limits and sit on top of billing, not instead of it. As of
the first live run on 2026-09-10 the key answers 429 "prepayment credits are
depleted" for every model, so none of the above applies yet; the project's
billing at https://ai.studio/projects has to be settled first. The code treats
that as a distinct, non-retryable condition — see `is_account_error`.

`gemini-2.5-flash-lite` is retired (404, "no longer available to new users") and
must not appear in a fallback chain.

Twenty judge calls a day forces a three-stage cascade. An earlier estimate of
250 came from third-party blogs and was wrong by more than tenfold; a design
built on it would not have run. Each stage now draws on a different quota:

| Stage | Model | Calls/day | Share of its quota |
|---|---|---|---|
| extract | gemini-3.1-flash-lite | ~83 | 17% |
| rough score | gemini-3.5-flash-lite | ~65 | 13% |
| final judge | gemini-3.5-flash | ≤12 | 60% |

Only the final stage sees the full resume against the full posting; the rough
stage scores against a short profile summary. Finalists are the vacancies
scoring at or above `min_score`, capped at `final_judge_limit` — deliberately
below 20, so a retry or a manual re-run cannot exhaust the day. If the judge's
quota is gone, the report carries the rough scores and says so.

`pacing.py` checks the counters before each call and waits if needed, so 429
should be rare by construction.

When a 429 does arrive, the error code distinguishes the cause:

| Code | Meaning | Handling |
|---|---|---|
| `rate_limit_exceeded` | per-minute limit | exponential backoff, retry |
| `too_many_requests` | burst | exponential backoff, retry |
| `quota_exceeded` | daily quota gone | drop this model for today, use fallback |

If every model is out of daily quota, the run stops, writes the report with what
it has, and marks the unprocessed vacancies so tomorrow picks them up first.

This replaces the current behaviour in `local_connectors/llm.py`, where `"429"`
sits in `_RETRYABLE` and `max_attempts=4` burns four requests per vacancy against
an already-empty quota.

A per-run call budget in config caps total LLM usage regardless.

## Report

`reports/YYYY-MM-DD.md`, directory gitignored.

Header with counts, then scored vacancies descending, each with company, work
format, salary, link and the LLM's reasons. Rejects follow in a collapsed
`<details>` block, one line each with the reason, so a wrong filter rule is
visible immediately. A footer lists anything that failed (network, quota, parse)
— vacancies are never dropped silently.

## Configuration

`config.toml`, read with stdlib `tomllib`: search preferences, listing query,
`count_days`, per-model quota limits, call budget, report score threshold.
Criteria change without touching code.

Secrets: `GOOGLE_API_KEY` via `--env-file`, never baked into the image. `data/`
mounted read-only; `state/` and `reports/` mounted so they survive container
recreation.

## Testing

TDD. `pytest` is already in `requirements-dev.txt`.

The 20 hand-picked reference vacancies become a regression fixture with a plain
assertion: **the filter must not reject any of them**. It fails today on two,
which is the first red test.

Rules in `core/logic.py` are pure functions tested on fixtures. The scraper is
tested against saved HTML. The LLM is never called in tests — the scorer is
injected into the pipeline and replaced with a fake.

## Cleanup carried out as part of this work

- `core/logic.py:apply_reply_updates` assigns fields to themselves and does
  nothing; remove it.
- `core/ports.py` declares unused `MessageSender`/`MessageReceiver`, and
  `VacancySource.fetch_vacancies` stops matching reality once the source is split.

## Out of scope

Non-Profesia job boards; learning the filter from user reactions; notifications.
