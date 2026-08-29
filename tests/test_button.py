"""Tests for the ORLEN Paczka refresh button."""
from unittest.mock import AsyncMock, MagicMock

from custom_components.orlen_paczka.button import ORLENPaczkaRefreshButton


async def test_refresh_button_requests_refresh():
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.runtime_data.coordinator.async_request_refresh = AsyncMock()

    button = ORLENPaczkaRefreshButton(entry)
    assert button.unique_id == "e1_refresh"

    await button.async_press()
    entry.runtime_data.coordinator.async_request_refresh.assert_awaited_once()
