"""job-apply-tracker's API over plain httpx.

The token travels in a header, not the URL, but errors are still scrubbed of
it: an exception text is the one thing that reliably ends up in a log.
"""

from typing import Any

import httpx

from local_connectors.telegram import redact

INGEST_PATH = "/api/v1/vacancies/"
TO_CHECK_PATH = "/api/v1/vacancies/to-check/"
CLOSED_PATH = "/api/v1/vacancies/closed/"


class TrackerError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        """HTTP status, or None when the request never got an answer."""


class TrackerClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    def push(self, vacancies: list[dict]) -> list[dict]:
        return self._call("POST", INGEST_PATH, {"vacancies": vacancies}).get("results", [])

    def offers_to_check(self) -> list[dict]:
        return self._call("GET", TO_CHECK_PATH).get("vacancies", [])

    def report_closed(self, closed: list[dict]) -> list[dict]:
        return self._call("POST", CLOSED_PATH, {"closed": closed}).get("results", [])

    def _call(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                self._base + path,
                json=body,
                headers={"Authorization": f"Token {self._token}"},
            )
        except httpx.HTTPError as error:
            # "from None": never let a chained transport error carry request details.
            raise TrackerError(redact(f"{type(error).__name__}: {error}", self._token)) from None

        if response.status_code != 200:
            raise TrackerError(
                redact(f"{response.status_code} {response.reason_phrase}", self._token),
                status=response.status_code,
            )
        try:
            answer = response.json()
        except ValueError:
            raise TrackerError("200 but the answer is not JSON", status=200) from None
        if not isinstance(answer, dict):
            raise TrackerError("200 but the answer is not a JSON object", status=200)
        return answer
