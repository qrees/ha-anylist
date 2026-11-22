"""Tests for keeping lists available through failed AnyList refreshes."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.anylist.client import AnyListTimeoutError
from custom_components.anylist.const import DOMAIN
from custom_components.anylist.coordinator import (
    STALE_DATA_GRACE,
    AnyListCoordinator,
)
from .conftest import FakeAnyListClient, fake_item, fake_list

ENTITY_ID = "todo.anylist_groceries"


class FlakyClient(FakeAnyListClient):
    """Fake whose list downloads can be made to fail."""

    failing = False

    def get_lists(self) -> list[Any]:
        """Fail like a timed-out download when asked to."""
        if self.failing:
            raise AnyListTimeoutError("Timed out waiting for AnyList")
        return super().get_lists()


async def test_failed_refreshes_keep_entities_available_for_a_while(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Entities keep the last data through failed refreshes, until the grace ends."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AnyList",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    client = FlakyClient(
        lists=[fake_list("list-1", "Groceries", items=[fake_item("item-1", "Milk")])]
    )
    with patch("custom_components.anylist.AnyListClient.login", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert hass.states.get(ENTITY_ID).state == "1"

    client.failing = True
    for _ in range(3):
        freezer.tick(timedelta(minutes=4))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert not coordinator.last_update_success
        assert hass.states.get(ENTITY_ID).state == "1"

    # The entity still accepts writes. The write reaches AnyList; only its
    # readback fails, and that is reported to the caller.
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": ENTITY_ID, "item": "item-1", "status": "completed"},
            blocking=True,
        )
    assert ("cross_off_item", ("list-1", "item-1")) in client.calls
    assert hass.states.get(ENTITY_ID).state == "1"

    # 12 minutes since the last success: the grace ends at 15 without a refresh.
    freezer.tick(STALE_DATA_GRACE - timedelta(minutes=12) + timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "unavailable"

    client.failing = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_success_cancels_the_grace_expiry(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A refresh that succeeds within the grace keeps entities available."""
    calls = 0

    async def fetch() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise UpdateFailed("Timed out fetching AnyList data")
        return {"lists": [], "favourites": []}

    coordinator = AnyListCoordinator(
        hass,
        logging.getLogger(__name__),
        config_entry=None,
        name="test",
        update_method=fetch,
    )
    notified = 0

    def listener() -> None:
        nonlocal notified
        notified += 1

    await coordinator.async_refresh()
    unsubscribe = coordinator.async_add_listener(listener)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.data_usable
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    notified_before = notified

    freezer.tick(STALE_DATA_GRACE + timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert notified == notified_before
    assert coordinator.data_usable

    unsubscribe()
    await coordinator.async_shutdown()
