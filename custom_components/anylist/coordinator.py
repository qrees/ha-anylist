"""Data coordinator for the AnyList integration."""
from __future__ import annotations

from collections.abc import Callable
import copy
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

LocalChange = Callable[[dict[str, Any]], None]

# Writes often come in bursts (a voice command adding several items), so the
# refresh after a write waits this long and then covers the whole burst.
REFRESH_AFTER_WRITE_DELAY = 10


class AnyListCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that shows Home Assistant's own writes without waiting for a refresh.

    A refresh downloads the whole account, which takes seconds on large lists.
    After a successful write the change is applied to the current data right
    away, and one refresh follows a burst of writes in the background.

    A refresh that started before a write finished may return the list without
    it. Each change is therefore kept and applied again to the data of every
    refresh that started before the change, until a refresh starts after it.
    Changes must give the same result when applied twice.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the coordinator."""
        super().__init__(*args, **kwargs)
        self._change_count = 0
        self._pending_changes: list[tuple[int, LocalChange]] = []
        self._refresh_after_write = Debouncer(
            self.hass,
            self.logger,
            cooldown=REFRESH_AFTER_WRITE_DELAY,
            immediate=False,
            function=self.async_refresh,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data and keep local changes the server may not have sent yet."""
        changes_before_fetch = self._change_count
        data = await super()._async_update_data()
        self._pending_changes = [
            (number, change)
            for number, change in self._pending_changes
            if number > changes_before_fetch
        ]
        for _, change in self._pending_changes:
            change(data)
        return data

    @callback
    def async_apply_local_change(self, change: LocalChange) -> None:
        """Show a successful write now and schedule a refresh after the burst."""
        self._change_count += 1
        self._pending_changes.append((self._change_count, change))
        change(self.data)
        # Not async_set_updated_data: it cancels pending refreshes.
        self.async_update_listeners()
        self.hass.async_create_background_task(
            self._refresh_after_write.async_call(), name="anylist refresh after write"
        )

    async def async_shutdown(self) -> None:
        """Cancel any scheduled refresh."""
        self._refresh_after_write.async_shutdown()
        await super().async_shutdown()


def _find_list(data: dict[str, Any], list_id: str) -> Any | None:
    """Return a shopping list from coordinator data."""
    return next(
        (
            shopping_list
            for shopping_list in data.get("lists", [])
            if shopping_list.id == list_id
        ),
        None,
    )


def _find_item(data: dict[str, Any], list_id: str, item_id: str) -> Any | None:
    """Return a list item from coordinator data."""
    shopping_list = _find_list(data, list_id)
    if shopping_list is None:
        return None
    return next((item for item in shopping_list.items if item.id == item_id), None)


def set_item_checked(list_id: str, item_id: str, checked: bool) -> LocalChange:
    """Return a change that checks off or unchecks an item."""

    def change(data: dict[str, Any]) -> None:
        if (item := _find_item(data, list_id, item_id)) is not None:
            item.is_checked = checked

    return change


def rename_item(list_id: str, item_id: str, name: str) -> LocalChange:
    """Return a change that renames an item."""

    def change(data: dict[str, Any]) -> None:
        if (item := _find_item(data, list_id, item_id)) is not None:
            item.name = name

    return change


def add_item(list_id: str, new_item: Any, at_top: bool) -> LocalChange:
    """Return a change that adds an item unless the list already has it."""

    def change(data: dict[str, Any]) -> None:
        shopping_list = _find_list(data, list_id)
        if shopping_list is None or any(
            item.id == new_item.id for item in shopping_list.items
        ):
            return
        # Each refresh gets its own copy; later changes edit the item in place.
        shopping_list.items.insert(
            0 if at_top else len(shopping_list.items), copy.copy(new_item)
        )

    return change


def delete_items(list_id: str, item_ids: list[str]) -> LocalChange:
    """Return a change that deletes items."""
    deleted = set(item_ids)

    def change(data: dict[str, Any]) -> None:
        if (shopping_list := _find_list(data, list_id)) is not None:
            shopping_list.items = [
                item for item in shopping_list.items if item.id not in deleted
            ]

    return change


def move_item(list_id: str, item_id: str, previous_item_id: str | None) -> LocalChange:
    """Return a change that moves an item after another one, or to the top."""

    def change(data: dict[str, Any]) -> None:
        shopping_list = _find_list(data, list_id)
        if shopping_list is None:
            return
        items = shopping_list.items
        item = next((item for item in items if item.id == item_id), None)
        if item is None:
            return
        items.remove(item)
        if previous_item_id is None:
            items.insert(0, item)
            return
        index = next(
            (i for i, other in enumerate(items) if other.id == previous_item_id),
            None,
        )
        items.insert(len(items) if index is None else index + 1, item)

    return change
