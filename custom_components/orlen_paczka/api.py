"""ORLEN Paczka's public JSONP tracking client."""
from __future__ import annotations

import json
from typing import Any

import aiohttp

from .const import TRACKING_API_URL


class ORLENPaczkaApiError(Exception):
    """Raised when an ORLEN Paczka API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"ORLEN Paczka API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class ORLENPaczkaApiClient:
    """Client for the public ORLEN Paczka tracking endpoint.

    No authentication: the endpoint is keyed on the tracking code alone and
    responds as ``callback(<JSON>);``. The wrapper is data transport, never
    JavaScript to execute.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the raw body for a populated response, or ``None`` for the
        semantic no-data envelope. Any malformed wrapper or non-200 response
        raises :class:`ORLENPaczkaApiError`; network errors propagate as
        ``aiohttp.ClientError``.
        """
        async with self._session.get(
            TRACKING_API_URL,
            params={"id": tracking_code, "jsonp": "callback"},
            timeout=20,
        ) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                raise ORLENPaczkaApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise ORLENPaczkaApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            body = await response.text()

        if not body.startswith("callback(") or not body.endswith(");"):
            raise ORLENPaczkaApiError("unexpected JSONP wrapper")
        try:
            payload = json.loads(body[len("callback(") : -2])
        except (TypeError, ValueError) as err:
            raise ORLENPaczkaApiError(f"unparseable JSONP body ({err})") from err
        if not isinstance(payload, dict):
            raise ORLENPaczkaApiError("unexpected body (not a JSON object)")

        if payload.get("err") == 1003:
            # Config flow rejects AD codes, but retain a defensively received
            # 1003 as an unknown parcel rather than turning a whole refresh
            # into a failure. The coordinator normalises and warns once.
            return {"number": tracking_code, "_orlen_error": "1003"}
        if payload.get("status") == "" and not payload.get("history"):
            return None
        if payload.get("status") != "OK" or not isinstance(payload.get("history"), list):
            raise ORLENPaczkaApiError("unexpected response envelope")
        return payload
