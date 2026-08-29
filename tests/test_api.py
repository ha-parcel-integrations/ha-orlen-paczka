"""Tests for the ORLEN Paczka JSONP API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.orlen_paczka.api import ORLENPaczkaApiClient, ORLENPaczkaApiError

from .payloads import ACTIVE_CODE, active_sample


def _session_returning(status: int, body: str) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = {}
    response.text = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _jsonp(payload: object) -> str:
    return f"callback({json.dumps(payload)});"


async def test_get_parcel_returns_populated_jsonp_body():
    session = _session_returning(200, _jsonp(active_sample()))
    parcel = await ORLENPaczkaApiClient(session).async_get_parcel(ACTIVE_CODE)
    assert parcel["number"] == ACTIVE_CODE
    assert session.get.call_args.kwargs["params"] == {"id": ACTIVE_CODE, "jsonp": "callback"}
    assert session.get.call_args.kwargs["timeout"] == 20


@pytest.mark.parametrize("payload", [{"status": "", "history": [], "truckNo": "Brak danych"}])
async def test_get_parcel_returns_none_for_semantic_no_data(payload):
    client = ORLENPaczkaApiClient(_session_returning(200, _jsonp(payload)))
    assert await client.async_get_parcel(ACTIVE_CODE) is None


async def test_get_parcel_marks_unsupported_allegro_response_for_safe_retention():
    client = ORLENPaczkaApiClient(_session_returning(200, _jsonp({"err": 1003})))
    assert await client.async_get_parcel(ACTIVE_CODE) == {
        "number": ACTIVE_CODE,
        "_orlen_error": "1003",
    }


@pytest.mark.parametrize("body", ["not jsonp", "other({});", "callback(not-json);", "callback([]);"])
async def test_get_parcel_rejects_malformed_or_non_object_jsonp(body):
    client = ORLENPaczkaApiClient(_session_returning(200, body))
    with pytest.raises(ORLENPaczkaApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_get_parcel_rejects_unexpected_envelope():
    client = ORLENPaczkaApiClient(_session_returning(200, _jsonp({"status": "wat"})))
    with pytest.raises(ORLENPaczkaApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_get_parcel_raises_on_error_status():
    client = ORLENPaczkaApiClient(_session_returning(500, ""))
    with pytest.raises(ORLENPaczkaApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_get_parcel_propagates_network_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await ORLENPaczkaApiClient(session).async_get_parcel(ACTIVE_CODE)
