import json

import httpx
import pytest

from core.ports import VacancyTracker
from local_connectors.tracker import TrackerClient, TrackerError

TOKEN = "fake-token"
BASE = "http://job-apply-tracker:8000"
ITEM = {"external_id": "O1", "title": "Junior Dev", "score": 70, "score_stage": "final"}


def client_with(handler, base=BASE) -> TrackerClient:
    return TrackerClient(base, TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))


def answering(results, status=200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(status, json={"results": results})

    return seen, handler


def test_the_client_satisfies_the_port():
    _, handler = answering([])
    assert isinstance(client_with(handler), VacancyTracker)


def test_a_push_posts_the_batch_with_the_token_header():
    seen, handler = answering([{"external_id": "O1", "status": "created", "id": 5}])

    client_with(handler).push([ITEM])

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/vacancies/"
    assert request.headers["Authorization"] == f"Token {TOKEN}"
    assert json.loads(request.content) == {"vacancies": [ITEM]}


def test_a_trailing_slash_in_the_base_url_is_harmless():
    seen, handler = answering([])
    client_with(handler, base=BASE + "/").push([])
    assert seen[0].url.path == "/api/v1/vacancies/"


def test_the_results_are_returned():
    results = [{"external_id": "O1", "status": "updated", "id": 5}]
    _, handler = answering(results)
    assert client_with(handler).push([ITEM]) == results


def test_a_refusal_is_an_error_with_its_status():
    _, handler = answering([], status=401)
    with pytest.raises(TrackerError) as raised:
        client_with(handler).push([ITEM])
    assert raised.value.status == 401


def test_a_network_failure_never_shows_the_token():
    def handler(request):
        raise httpx.ConnectError(f"cannot connect, header was Token {TOKEN}")

    with pytest.raises(TrackerError) as raised:
        client_with(handler).push([ITEM])
    assert TOKEN not in str(raised.value)
    assert raised.value.status is None
    assert raised.value.__cause__ is None


def test_offers_to_check_gets_the_list_with_the_token_header():
    offers = [{"external_id": "O5330945", "url": "https://www.profesia.sk/praca/builtmind/O5330945"}]
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"vacancies": offers})

    assert client_with(handler).offers_to_check() == offers
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/vacancies/to-check/"
    assert seen[0].headers["Authorization"] == f"Token {TOKEN}"


def test_report_closed_posts_the_items():
    items = [{"external_id": "O1", "seen_at": "2026-10-27T08:20:00+00:00"}]
    results = [{"external_id": "O1", "status": "closed"}]
    seen, handler = answering(results)

    assert client_with(handler).report_closed(items) == results
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/v1/vacancies/closed/"
    assert seen[0].headers["Authorization"] == f"Token {TOKEN}"
    assert json.loads(seen[0].content) == {"closed": items}


def test_a_refused_to_check_is_an_error_with_its_status():
    _, handler = answering([], status=401)
    with pytest.raises(TrackerError) as raised:
        client_with(handler).offers_to_check()
    assert raised.value.status == 401


def test_a_garbled_to_check_answer_is_an_error():
    def handler(request):
        return httpx.Response(200, text="<html>proxy page</html>")

    with pytest.raises(TrackerError):
        client_with(handler).offers_to_check()


def test_a_garbled_answer_is_an_error():
    def handler(request):
        return httpx.Response(200, text="<html>proxy page</html>")

    with pytest.raises(TrackerError):
        client_with(handler).push([ITEM])
