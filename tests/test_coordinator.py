"""Tests for showing writes locally before AnyList is refreshed."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.anylist.const import DOMAIN
from custom_components.anylist.coordinator import (
    REFRESH_AFTER_WRITE_DELAY,
    AnyListCoordinator,
    add_item,
    delete_items,
    move_item,
    rename_item,
    set_item_checked,
)
from .conftest import FakeAnyListClient, fake_item, fake_list

ENTITY_ID = "todo.anylist_groceries"


class ServerFakeClient(FakeAnyListClient):
    """Fake whose reads return copies of the server state, like the real client."""

    def get_lists_and_favourites(self) -> tuple[list[Any], list[Any]]:
        """Return a snapshot of the server's lists."""
        self.calls.append(("get_lists_and_favourites", ()))
        return copy.deepcopy(self.lists), list(self.favourites)

    def cross_off_item(self, list_id: str, item_id: str) -> None:
        """Check off an item on the server."""
        super().cross_off_item(list_id, item_id)
        next(item for item in self.lists[0].items if item.id == item_id).is_checked = True


def _snapshot(shopping_list: Any) -> list[tuple[str, str, bool]]:
    """Return the ids, names and checked states of a list."""
    return [(item.id, item.name, item.is_checked) for item in shopping_list.items]


async def test_writes_show_at_once_and_one_refresh_follows_a_burst(
    hass: HomeAssistant,
) -> None:
    """Writes don't wait for a refresh, and a burst of them is refreshed once."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AnyList",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    client = ServerFakeClient(
        lists=[
            fake_list(
                "list-1",
                "Groceries",
                items=[fake_item("item-1", "Milk"), fake_item("item-2", "Eggs")],
            )
        ]
    )
    with patch("custom_components.anylist.AnyListClient.login", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "2"

    client.calls.clear()
    for item_id in ("item-1", "item-2"):
        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": ENTITY_ID, "item": item_id, "status": "completed"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "0"
    assert ("get_lists_and_favourites", ()) not in client.calls

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=REFRESH_AFTER_WRITE_DELAY + 1)
    )
    await hass.async_block_till_done()

    assert client.calls.count(("get_lists_and_favourites", ())) == 1
    assert hass.states.get(ENTITY_ID).state == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_refresh_started_before_a_write_keeps_it(hass: HomeAssistant) -> None:
    """A refresh that missed a write doesn't undo it; a later one is trusted."""
    server = fake_list("list-1", "Groceries", items=[fake_item("item-1", "Milk")])
    started = asyncio.Event()
    release = asyncio.Event()
    block = False

    async def fetch() -> dict[str, Any]:
        snapshot = copy.deepcopy(server)
        if block:
            started.set()
            await release.wait()
        return {"lists": [snapshot], "favourites": []}

    coordinator = AnyListCoordinator(
        hass,
        logging.getLogger(__name__),
        config_entry=None,
        name="test",
        update_method=fetch,
    )
    await coordinator.async_refresh()

    block = True
    refresh = hass.async_create_task(coordinator.async_refresh())
    await started.wait()
    coordinator.async_apply_local_change(set_item_checked("list-1", "item-1", True))
    assert _snapshot(coordinator.data["lists"][0]) == [("item-1", "Milk", True)]

    # The refresh downloaded the list before the write reached AnyList.
    release.set()
    await refresh
    assert _snapshot(coordinator.data["lists"][0]) == [("item-1", "Milk", True)]

    # A refresh that starts after the write shows what AnyList has.
    block = False
    await coordinator.async_refresh()
    assert _snapshot(coordinator.data["lists"][0]) == [("item-1", "Milk", False)]

    await coordinator.async_shutdown()


def test_local_changes_give_the_same_result_when_applied_twice() -> None:
    """Each change can be applied to data that already has it."""
    data = {
        "lists": [
            fake_list(
                "list-1",
                items=[
                    fake_item("item-1", "Milk"),
                    fake_item("item-2", "Eggs"),
                    fake_item("item-3", "Bread"),
                ],
            )
        ]
    }
    changes = [
        set_item_checked("list-1", "item-1", True),
        rename_item("list-1", "item-2", "Free-range eggs"),
        add_item("list-1", fake_item("item-4", "Apples"), at_top=True),
        move_item("list-1", "item-3", "item-1"),
        delete_items("list-1", ["item-2"]),
        # Changes for other lists or missing items are ignored.
        set_item_checked("list-2", "item-1", True),
        rename_item("list-1", "missing", "Nothing"),
        move_item("list-1", "missing", None),
    ]

    for _ in range(2):
        for change in changes:
            change(data)

    assert _snapshot(data["lists"][0]) == [
        ("item-4", "Apples", False),
        ("item-1", "Milk", True),
        ("item-3", "Bread", False),
    ]
