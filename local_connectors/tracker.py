"""job-apply-tracker's ingest API over plain httpx.

The token travels in a header, not the URL, but errors are still scrubbed of
it: an exception text is the one thing that reliably ends up in a log.
"""

from typing import Any

import httpx

from local_connectors.telegram import redact

INGEST_PATH = "/api/v1/vacancies/"


class TrackerError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        """HTTP status, or None when the request never got an answer."""


class TrackerClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        self._url = base_url.rstrip("/") + INGEST_PATH
        self._token = token
        self._client = client or httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    def push(self, vacancies: list[dict]) -> list[dict]:
        try:
            response = self._client.post(
                self._url,
                json={"vacancies": vacancies},
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
            body: dict[str, Any] = response.json()
        except ValueError:
            raise TrackerError("200 but the answer is not JSON", status=200) from None
        return body.get("results", [])
