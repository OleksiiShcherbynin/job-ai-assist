# Telegram bot — design

Status: approved 2026-09-15. Extends the daily vacancy run
(`2026-09-10-daily-vacancy-run-design.md`), which listed notifications as out of
scope.

## Goal

The morning report reaches the user's phone without opening the editor: a short
summary plus the full report file, in the language the user picked, sent by the
bot as soon as the day's run finishes. Past reports can be requested on demand.

## Decisions taken

| Question | Decision |
|---|---|
| What is sent | Summary message (counts + strong matches) and the full `.md` report attached |
| Language | Chosen once with buttons on `/start`, changeable by repeating `/start`; applies to summary and file |
| When | Right after the run, no time-of-day gate |
| Failed delivery | Stays queued in `state/`, retried until it succeeds |
| Who may use the bot | Only the chat in `TELEGRAM_CHAT_ID`; everyone else is ignored |
| Receiving commands | Long-polling thread inside the existing container |
| On demand | `/today` and `/report <date>` resend existing reports; never trigger a run |
| Library | None: plain `httpx`, already a dependency |

Rejected alternatives:

- A second compose service for the bot — two processes writing one SQLite file
  through a Docker Desktop bind mount on Windows, where SQLite locking is
  unreliable, plus another ~130 MB.
- Reading updates only on the 15-minute check — up to 15 minutes before `/start`
  or a button press gets an answer.
- A "not before 08:00" gate — the laptop hibernates overnight and the run
  happens after it is opened, so the gate would almost never matter. If the
  laptop is ever left on overnight, the message arrives around midnight; a gate
  can be added then.
- `python-telegram-bot` — its own asyncio loop and a dependency tree for five
  API methods.
- Addressing the user by `@username` — the Bot API cannot message a username,
  and a released username can be taken by someone else. The numeric chat id is
  stable.

## Facts this design rests on

- Telegram messages are capped at 4096 characters; the 2026-09-15 Russian report
  is ~30 KB. The report cannot be sent as text.
- Document captions are capped at 1024 characters, so the summary is a separate
  message rather than the file's caption.
- The Bot API URL carries the token (`https://api.telegram.org/bot<token>/...`),
  and `httpx` logs every request URL at INFO. With the current `basicConfig(INFO)`
  the token would land in `docker compose logs`.
- Long polling issues one request per ~50 s; at INFO that is a log line a minute.
- Container log for 2026-09-14/15: last check 21:44, laptop hibernated, first
  check after wake 09:10, report written 09:14. The run is deferred, not lost.

## User flow

1. The user puts `TELEGRAM_BOT_TOKEN` in `.env` and rebuilds the container.
2. The user sends `/start`. With `TELEGRAM_CHAT_ID` unset, the bot replies with
   the sender's numeric chat id and says to put it in `.env`. It answers nothing
   else in this state, so a stranger learns only their own id.
3. With `TELEGRAM_CHAT_ID` set and the container restarted, `/start` from that
   chat shows "Язык отчёта / Report language" with buttons **Русский** and
   **English**. Pressing one saves the choice and edits the message to confirm
   it. Any queued deliveries go out immediately.
4. Every day after the run: the summary message, then the report file in the
   chosen language.

Commands, all from the configured chat only:

| Input | Reply |
|---|---|
| `/start` | Language buttons |
| `/today` | Same as `/report` for today |
| `/report 14.09`, `/report 14.09.2026`, `/report 2026-09-14` | That day's summary and file |
| `/report` with an unparseable date | One-line hint with the accepted formats |
| Anything else | One-line list of commands |

`DD.MM` means the current year. `/today` and `/report` ask the user to press
`/start` first if no language is chosen yet.

For a day with a stored summary, `/report` sends summary and file. For a day
with a report file but no stored summary (runs before this feature), it sends
the file with a one-line caption saying the summary is unavailable. For a day
with no file: "no report for 14.09.2026"; for today before the run: "today's run
has not happened yet". On-demand sends never touch the delivery queue.

## Summary message

Telegram HTML parse mode (escaping is simpler and safer than MarkdownV2). Built
from the `RunReport` at run time, reusing `_PHRASES` and `_facts` from
`app/report.py`:

```
<b>Вакансии — 2026-09-16</b>
Просмотрено 59 · оценено 26 · выше 40: 7 · отсеяно 5

<b>86</b> · <a href="https://www.profesia.sk/...">Junior Python Developer</a>
Softec · home office · 1 400–1 800 EUR/mesiac
```

- Only matches at or above `min_score`, highest first, without reasons — the
  reasons are in the file.
- Titles, company and salary pass through `html.escape`.
- Entries are added while the message plus the tail stays within 4096
  characters; the tail reads "…и ещё N — в файле" / "…and N more — in the file".
- No strong matches: the header, counts and the existing "nothing scored above
  the threshold today" line. The message is still sent, so silence always means
  something is wrong.

## Architecture

```
core/
  ports.py              + Messenger protocol
local_connectors/
  telegram.py           TelegramClient: the only code that knows the Bot API
app/
  bot.py                Bot: handle_update, deliver_pending, listen
  report.py             + render_telegram_summary(report, min_score, language)
  store.py              + deliveries and bot_state tables
  main.py               queue after write_reports, start listener, wake event
.env.example            + TELEGRAM_BOT_TOKEN=, TELEGRAM_CHAT_ID=
```

No change to `config.toml`, `compose.yaml` (it already reads `.env`), the
Dockerfile or the requirements.

### `Messenger` port

Five operations, mirroring the Bot API methods used:

| Operation | Bot API |
|---|---|
| `get_updates(offset, timeout)` | `getUpdates` |
| `send_message(chat_id, html, buttons=None)` | `sendMessage` with optional inline keyboard |
| `send_document(chat_id, path, caption=None)` | `sendDocument` (multipart) |
| `answer_callback(callback_id)` | `answerCallbackQuery` |
| `edit_message(chat_id, message_id, html)` | `editMessageText` |

Button callback data: `lang:ru`, `lang:en`.

`TelegramClient` raises `TelegramError` for every failure, carrying the HTTP
status (or none for a network error), Telegram's `description`, and
`retry_after` when given. Its message has the token replaced with `***`, and it
is raised `from None` so the original `httpx` exception — whose text includes the
URL — is not printed as context.

### State

Two tables in `state/seen.db`:

```sql
CREATE TABLE IF NOT EXISTS deliveries (
    report_date TEXT PRIMARY KEY,
    summary_en  TEXT NOT NULL,
    summary_ru  TEXT NOT NULL,
    sent_at     TEXT            -- NULL = queued
);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,     -- 'language', 'update_offset'
    value TEXT NOT NULL
);
```

- A `deliveries` row is written right after `write_reports`, only when
  `TELEGRAM_BOT_TOKEN` is set, so enabling the bot later does not dump a backlog
  of every day since the first run.
- Writing a row for a date that already exists (a `--force` rerun) replaces both
  summaries and resets `sent_at` to NULL: the corrected report is sent again.
- Queued rows go out oldest first. Each day's report holds that day's vacancies
  (`count_days=1`), so older ones are not superseded by newer ones.
- `update_offset` is stored after each handled update. Without it a restart
  would re-read unconfirmed updates and answer an old `/start` again.
- Summaries are stored rather than re-derived from the Markdown file: delivery
  may happen after a restart when the `RunReport` is gone, `/report` must return
  the same summary the morning message had, and parsing our own Markdown back
  would couple two formats.

## Concurrency

- **Main thread** — the existing loop. It is the only sender of queued
  deliveries: right after a run, and on every iteration. `time.sleep` becomes
  `wake.wait(IDLE_CHECK_SECONDS)` on a `threading.Event`, then `wake.clear()`.
- **Listener thread** — daemon, started only when the token is set and not in
  `--once` mode. Long-polls `getUpdates` (timeout 50 s, HTTP read timeout 60 s),
  handles commands, sends on-demand replies itself, and calls `wake.set()` after
  a language is chosen so queued deliveries leave within seconds.
- Queued deliveries have a single sender, so one cannot go out twice. An
  on-demand `/today` racing the morning delivery can produce a duplicate; that is
  harmless.
- Each thread opens its own `Store`: a `sqlite3` connection may not cross
  threads, and two connections in one process writing single rows do not
  contend beyond the default 5 s busy timeout.
- `--once`: no listener; one delivery attempt after the run. A failed delivery
  does not change the exit code of a successful run.

`deliver_pending` sends nothing unless token, chat id and language are all set.

## Failure handling

The bot must never break a run: every bot call in `main.py` is wrapped, logged,
and swallowed.

| Situation | Behaviour |
|---|---|
| Network error, 5xx, 429 while delivering | Row stays queued; retried next iteration (≤15 min, or on wake) |
| Same, in the listener | Back off 5 s, doubling to 300 s, reset on success. One warning when an outage starts, one info line when it ends |
| 401 Unauthorized | One error line: `TELEGRAM_BOT_TOKEN` rejected, check `.env`. Listener stops; runs continue; deliveries stay queued |
| 403 (bot blocked by the user) | Row stays queued, warning logged |
| Summary sent, document failed | Whole delivery stays queued; the summary may arrive twice on retry. Accepted over tracking two flags |
| Report file missing when delivering | Summary sent with a note that the file is gone; row marked sent |
| Laptop hibernates mid-poll | Connection error on wake → normal backoff path |

## Security

- The token and chat id come only from the environment (`.env`, gitignored,
  read by compose). Never in `config.toml`, code, tests or the image.
- The `httpx` logger is raised to WARNING in `main.py`: it removes both the
  token-bearing URLs and the once-a-minute polling noise. Gemini and Profesia
  request lines disappear with it; they were noise too.
- As a last line, a logging filter installed when the token is set replaces the
  token with `***` in every record, including formatted tracebacks.
- Updates and callback queries are handled only when `chat.id` equals
  `TELEGRAM_CHAT_ID`; others are dropped without a reply. The one exception is
  `/start` while the chat id is unset, answered with the sender's own id.
- `/report` dates are parsed by strict patterns into a `date`; the file path is
  built from `date.isoformat()`, never from the user's text.
- Test tokens are obviously fake strings that gitleaks' Telegram rule does not
  match.

## Testing

TDD, no network, no real bot. `Messenger` is replaced with a fake that records
calls and can be told to fail.

- **Summary** — HTML escaping of `<`, `>`, `&` in titles; the 4096 cap with the
  "and N more" tail; the no-strong-matches day; both languages.
- **Store** — queue order; `sent_at` set on success; `--force` rerun resets a
  sent row; `bot_state` round trip.
- **Commands** — stranger ignored; `/start` with chat id unset replies with the
  id; `/start` shows buttons; callback saves language, edits the message and
  sets `wake`; `/today` and `/report` in all three date formats; bad date; day
  without summary sends file only; day without file; no language chosen; offset
  advances.
- **Delivery** — nothing sent without language or chat id; failure keeps the
  row queued; success marks it sent; the file matches the chosen language;
  oldest first; missing file.
- **Client boundary** — a failing request raises `TelegramError` whose text does
  not contain the token.
- **Logging** — with the token set, a logged exception containing it shows `***`.
- **Main** — no listener and no queue rows without the token; the existing
  `main` tests pass unchanged.

Live check without spending Gemini quota: send `/report 15.09` against the
existing report file, and push one summary built from a hand-made `RunReport`
through the real bot.

## Setup

1. `TELEGRAM_BOT_TOKEN=...` in `.env`.
2. `docker compose up -d --build`.
3. `/start` to the bot → put the returned id in `.env` as `TELEGRAM_CHAT_ID`.
4. `docker compose up -d` (recreates the container so it reads the new `.env`;
   `restart` does not re-read `env_file`).
5. `/start` again → pick a language.

## Out of scope

Time-of-day delivery gate; summaries for reports written before this feature;
more than one recipient; Telegram command menu (`setMyCommands`); reasons in the
summary message; webhook mode.
