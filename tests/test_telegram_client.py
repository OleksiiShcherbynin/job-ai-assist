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
