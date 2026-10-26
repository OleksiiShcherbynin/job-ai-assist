# Закрытые объявления Profesia сообщаются трекеру — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ: superpowers:subagent-driven-development (рекомендуется) или superpowers:executing-plans — выполнять план задача за задачей. Шаги размечены чекбоксами (`- [ ]`).

> Перевод [английского плана](2026-10-26-auto-close.md). При расхождениях главный — английский.

**Цель:** раз в день, после рана и отправки в трекер, job-ai-assist спрашивает у job-apply-tracker, какие офферы Profesia важны, загружает страницу каждого объявления и сообщает обратно о закрытых — без вызовов Gemini и никогда не замедляя и не ломая ран и отчёт в Telegram.

**Архитектура:** те же ports & adapters, что у отправки Этапа 2. Порт `VacancyTracker` получает `offers_to_check` / `report_closed` (адаптер `local_connectors/tracker.py`); порт `VacancySource` — метод сырой загрузки страницы `fetch_page` (адаптер `ProfesiaSource`); решение «закрыто/открыто» — чистый код в `core/closure.py`; отметка «раз в день» и ожидающий отчёт о закрытых — в SQLite-хранилище; `app/closure_check.py` обходит список, `app/main.py` подключает его после отправки.

**Стек:** Python 3.12, httpx (уже закреплён), SQLite, pytest с `httpx.MockTransport` и фейковыми портами (без сети в тестах).

**Спека:** репозиторий job-apply-tracker, `docs/superpowers/specs/2026-10-26-auto-close-design.md`, разделы «Как распознаётся закрытие», «Часть B», «Тестирование (Часть B)». Часть A (сторона трекера) уже работает.

## Глобальные ограничения

- Контракт, тот же токен и заголовок, что у отправки (`Authorization: Token <ключ>`):
  - `GET {TRACKER_API_URL}/api/v1/vacancies/to-check/` → `{"vacancies": [{"external_id": "O5330945", "url": "https://www.profesia.sk/praca/builtmind/O5330945"}]}`.
  - `POST {TRACKER_API_URL}/api/v1/vacancies/closed/`, тело `{"closed": [{"external_id", "seen_at"}]}`, **≤ 200 элементов**, `seen_at` в ISO 8601 со смещением → `{"results": [{"external_id", "status": "closed"|"unchanged"|"unknown"|"invalid"}]}`. Повторы безвредны (`unchanged`).
  - Лимит 60 запросов в час на пользователя, общий с отправкой: проверка делает не больше 1 GET + ceil(n/200) POST в день.
- **закрыто ⇔ HTTP 200 и на странице есть `ukončila zverejnenie ponuky`.** Всё остальное (ошибка загрузки, не 200, нет `<h1>`) — «не знаю»: пропуск на сегодня, никогда не отчёт.
- Загружаются только адреса Profesia (`www.profesia.sk`, голый `profesia.sk`); остальные из списка пропускаются без запроса.
- Около одного запроса в **2 с** между загрузками страниц; пауза подменяемая, в тестах не спим.
- Раз в день, с отметкой в хранилище: день **не** отмечается, если `GET to-check` упал; отмечается сразу после загрузки страниц, закрытые id сохраняются как ожидающий отчёт, а `POST closed` повторяется при следующих проверках из этого списка. Profesia никогда не обходится дважды за день.
- Работает только при заданных `TRACKER_API_URL`/`TRACKER_API_TOKEN`; после доставки в Telegram и отправки в трекер, при каждой проверке и под `--once`; не зависит от успеха рана.
- Без вызовов Gemini. Любой сбой → один WARNING, никаких исключений из цикла, ран и Telegram не страдают.
- Фикстуры: обрезанные реальные объявления Profesia (одно закрытое, одно открытое), токены вычищены (правило репозитория: сырой HTML Profesia не коммитить — в страницах `offer-criteria-api-token`/`region-api-token`, gitleaks блокирует).
- Правила репозитория: TDD, `py -3.12 -m pytest tests/ -q` зелёный без сети; никогда `git add -A` (WIP ноутбука пользователя, `CLAUDE.md`, `.claude/` не трогаем); коммиты — простые повелительные предложения; токен никогда не печатается, не логируется и не попадает в командную строку.

## На что смотреть при ревью

1. Profesia отдаёт капчу / страницу техработ с 200 (нет `<h1>`, нет уведомления) → все офферы на сегодня «не знаю», ничего не сообщено, день отмечен, завтра проверка снова — Задача 1 (`posting_state`) и Задача 5.
2. Контейнер трекера ещё не поднялся, когда стартует job-ai-assist → `GET to-check` падает, день не отмечен, следующая 15-минутная проверка пробует снова и обходит Profesia один раз — Задача 5.
3. `POST closed` падает после обхода (трекер перезапускается, лимит 429) → закрытые id переживают это в хранилище, отправляются при следующей проверке без запросов к Profesia и уходят из хранилища только после ответа — Задачи 3 и 5.
4. В списке to-check адрес не с Profesia (заявка, введённая вручную) или хост вида `profesia.sk.evil.com` → запроса к нему нет — Задача 5.
5. Ответ трекера короче отправленного или с другими id → несопоставленные остаются в очереди; `invalid`/`unknown` выбрасываются с одной строкой в логе (повтор их не исправит) — Задача 5.

---

### Задача 1: Чистое распознавание — `core/closure.py` + фикстуры

**Файлы:** создать `core/closure.py`, `tests/test_closure.py`, `tests/fixtures/profesia_posting_closed.html`, `tests/fixtures/profesia_posting_open.html`.

**Интерфейсы (даёт):**
- `CLOSED_NOTICE = "ukončila zverejnenie ponuky"`.
- `is_closed(html: str) -> bool` — True только когда уведомление есть в тексте страницы (пробелы внутри фразы схлопываются, перенос строки внутри неё не мешает).
- `posting_state(status: int, html: str) -> Literal["closed", "open", "unknown"]` — `unknown` при `status != 200` или без тега `<h1`; `closed` при `is_closed(html)`; иначе `open`.

- [ ] **Шаг 1: Фикстуры.** Скриптом в scratchpad (не inline `-c`: heredoc в Bash портит обратные слэши) взять из `state/seen.db` адреса сентябрьских офферов, загрузить с заголовками из `local_connectors/vacancy_source.py`; оставить первую страницу с уведомлением и первую без него, но с кнопкой «Reagovať na ponuku». Обрезать до `<html><body>` + `<h1>` + `div.alert.alert-warning` (закрытая) / кнопка (открытая) + короткий абзац описания; любые `*-api-token` заменить на `REDACTED`; шапка-комментарий как у `profesia_listing.html`.
- [ ] **Шаг 2: Падающие тесты** в `tests/test_closure.py`: `test_the_closed_fixture_is_closed`; `test_the_open_fixture_is_open`; `test_the_notice_split_across_whitespace_still_counts`; `test_a_similar_phrase_without_diacritics_does_not_count`; `test_an_error_page_is_unknown_not_closed` (`posting_state(500, CLOSED) == "unknown"`); `test_a_page_without_a_title_is_unknown` (капча → `unknown`, п. 1 ревью); `test_an_open_posting_is_open` / `test_a_closed_posting_is_closed`.
- [ ] **Шаг 3:** `py -3.12 -m pytest tests/test_closure.py -q` → FAIL.
- [ ] **Шаг 4:** Реализовать `core/closure.py` (только stdlib).
- [ ] **Шаг 5:** весь набор зелёный.
- [ ] **Шаг 6:** Коммит «Recognise a closed Profesia posting from its page».

### Задача 2: Сырая загрузка страницы в источнике

**Файлы:** изменить `core/ports.py` (`VacancySource`), `local_connectors/vacancy_source.py`; тесты `tests/test_profesia_source.py`.

**Интерфейсы (даёт):** `VacancySource.fetch_page(url: str) -> tuple[int, str]` — код ответа и сырой HTML; `ValueError` для чужого хоста; на не-200 **не** бросает. `ProfesiaSource.fetch_page` — на существующем клиенте и заголовках, без паузы внутри.

- [ ] **Шаг 1: Падающие тесты:** `test_fetch_page_returns_status_and_raw_html`; `test_fetch_page_hands_back_a_non_200_instead_of_raising`; `test_fetch_page_refuses_a_host_outside_profesia`.
- [ ] **Шаги 2–4:** FAIL → реализация → зелёный.
- [ ] **Шаг 5:** Коммит «Fetch a posting page raw so a caller can read its status».

### Задача 3: Хранилище — отметка дня и ожидающий отчёт о закрытых

**Файлы:** изменить `app/store.py`; тесты `tests/test_store.py`.

**Интерфейсы (даёт):** таблица `closed_outbox(offer_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)`; `Store.closure_checked_on() -> date | None` (ключ `bot_state` `closure_checked_on`); `Store.finish_closure_check(day, closed, seen_at)` — один коммит: отметка дня + upsert id (`ON CONFLICT DO NOTHING`, первый `seen_at` остаётся); `Store.closed_pending(limit) -> list[Row]` (`offer_id`, `seen_at`, по `seen_at, offer_id`); `Store.closed_done(offer_ids)`.

- [ ] **Шаг 1: Падающие тесты:** `test_a_fresh_store_has_never_checked_closures`; `test_a_finished_closure_check_remembers_its_day_and_queues_the_closed_ids`; `test_a_closed_id_seen_again_keeps_its_first_seen_at`; `test_reported_closures_leave_the_queue`; `test_a_check_with_nothing_closed_still_marks_the_day`; `test_the_closure_marker_survives_reopening_the_database`.
- [ ] **Шаги 2–4:** FAIL → реализация → зелёный.
- [ ] **Шаг 5:** Коммит «Remember the day's closure check and what it found in the store».

### Задача 4: Порт и адаптер трекера — to-check и closed

**Файлы:** изменить `core/ports.py` (`VacancyTracker`), `local_connectors/tracker.py`; тесты `tests/test_tracker_client.py`.

**Интерфейсы (даёт):** `VacancyTracker.offers_to_check() -> list[dict]`; `VacancyTracker.report_closed(closed: list[dict]) -> list[dict]`. В `TrackerClient` — `TO_CHECK_PATH`, `CLOSED_PATH` и один приватный `_call(method, path, json=None) -> dict` с заголовком токена, разбором ошибок в `TrackerError` и вычисткой токена; `push` переезжает на него без изменения поведения.

- [ ] **Шаг 1: Падающие тесты:** `test_offers_to_check_gets_the_list_with_the_token_header`; `test_report_closed_posts_the_items`; `test_a_refused_to_check_is_an_error_with_its_status`; `test_a_garbled_to_check_answer_is_an_error`; `test_the_client_satisfies_the_port` проходит с выросшим протоколом.
- [ ] **Шаги 2–4:** FAIL → реализация → зелёный.
- [ ] **Шаг 5:** Коммит «Ask the tracker which offers to check and tell it which closed».

### Задача 5: Ежедневная проверка — `app/closure_check.py`

**Файлы:** создать `app/closure_check.py`, `tests/test_closure_check.py`.

**Интерфейсы:** `PAUSE_SECONDS = 2.0`, `BATCH_SIZE = 200`; `CheckResult(listed, fetched, closed, skipped, reported, settled)`; `check_closures(store, tracker, source, today, sleeper=None, now=None) -> CheckResult` (`None` → `time.sleep` / `datetime.now(timezone.utc)` в момент вызова). Порядок: (1) дослать ожидающее (`closed|unchanged|unknown|invalid` по совпавшему `external_id` уходят из очереди, `unknown`/`invalid` — WARNING; несопоставленные остаются; короткий ответ останавливает цикл); (2) день уже отмечен → выход; (3) `offers_to_check()`, `TrackerError` летит наружу, день не отмечен; (4) обход: чужие хосты пропуск, пауза перед каждой загрузкой кроме первой, ошибка загрузки → пропуск, `posting_state`; (5) `finish_closure_check`; (6) снова дослать ожидающее.

- [ ] **Шаг 1: Падающие тесты** (`FakeTracker`, `FakeSource`, список пауз): `test_closed_postings_are_reported_with_seen_at`; `test_the_day_is_marked_only_after_the_walk`; `test_a_failed_to_check_leaves_the_day_unmarked`; `test_a_failed_report_is_retried_later_without_refetching`; `test_a_second_call_the_same_day_does_nothing`; `test_non_profesia_urls_are_skipped_without_a_request`; `test_fetches_are_paced` (`[2.0, 2.0]`); `test_a_fetch_error_is_skipped_not_closed`; `test_a_page_without_a_title_is_skipped`; `test_unknown_and_invalid_answers_leave_the_queue_with_a_warning`; `test_a_short_answer_keeps_unmatched_items_pending`; `test_a_big_closed_list_goes_out_in_batches_of_200`.
- [ ] **Шаги 2–4:** FAIL → реализация → зелёный.
- [ ] **Шаг 5:** Коммит «Check the tracker's offers on Profesia once a day and report the closed ones».

### Задача 6: Подключение в ежедневный цикл

**Файлы:** изменить `app/main.py`; тесты `tests/test_main.py`.

**Интерфейсы:** `check_closed_postings(config, tracker, today) -> None` — открывает `Store` и `ProfesiaSource`, вызывает `check_closures`, закрывает оба; одна INFO-строка `checked %d postings on Profesia: %d closed, %d skipped; reported %d to the tracker`, когда что-то загружено или отправлено; 401/403 → существующая формулировка «rejected the token»; иначе WARNING `could not check closed postings; will retry on the next check (%s)`. Не бросает. В `main()` — сразу после `push_to_tracker`, когда трекер включён.

- [ ] **Шаг 1: Падающие тесты:** `test_closed_postings_are_reported_after_the_push`; `test_the_closure_check_runs_after_the_tracker_push`; `test_a_closure_check_failure_does_not_fail_the_run`; `test_the_closure_check_is_off_without_tracker_settings`; `test_the_closure_check_does_not_need_todays_run`.
- [ ] **Шаги 2–4:** FAIL → реализация → зелёный.
- [ ] **Шаг 5:** Коммит «Check for closed postings after every push without letting it hold up the run».

### Задача 7: Запуск вместе с пользователем

- [ ] **Шаг 1:** весь набор зелёный, bandit без HIGH, в фикстурах только `REDACTED`.
- [ ] **Шаг 2:** финальное независимое ревью ветки на субагенте fable; Critical/Important исправить с тестами.
- [ ] **Шаг 3:** push `feature/auto-close`; пользователь открывает PR, CI зелёный, мёржит.
- [ ] **Шаг 4:** пользователь: `docker compose up -d --build` с main. Проверка: `docker compose logs --since 30m daily-run | grep -i "closed postings\|checked"`; в трекере у закрытых — «Closed on Profesia». Квоту моделей ничто не тратит.
- [ ] **Шаг 5:** обновить оба досье (job-ai-assist `tracker.md`: NEXT → готово, как работает проверка, восстановление `DELETE FROM bot_state WHERE key='closure_checked_on'`; dj-jobs-web).
