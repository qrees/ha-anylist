"""Data coordinator for the AnyList integration."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

# AnyList downloads sometimes time out. Entities keep showing the last good
# data for this long after the last successful refresh before they become
# unavailable, so one slow download does not break automations and voice.
STALE_DATA_GRACE = timedelta(minutes=15)


class AnyListCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator whose data stays usable for a while after failed refreshes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the coordinator."""
        super().__init__(*args, **kwargs)
        self._last_success: datetime | None = None
        self._grace_timer: CALLBACK_TYPE | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data and track when the last refresh succeeded."""
        try:
            data = await super()._async_update_data()
        except Exception:
            self._schedule_grace_expiry()
            raise
        self._last_success = dt_util.utcnow()
        self._cancel_grace_timer()
        return data

    @property
    def data_usable(self) -> bool:
        """Return whether entities should show the current data.

        True after a successful refresh, and for STALE_DATA_GRACE after the
        last success while refreshes are failing.
        """
        if self.last_update_success:
            return True
        return (
            self.data is not None
            and self._last_success is not None
            and dt_util.utcnow() - self._last_success < STALE_DATA_GRACE
        )

    @callback
    def _schedule_grace_expiry(self) -> None:
        """Make entities re-check availability once the grace period ends.

        The base coordinator does not notify listeners after a second failure
        in a row, so without this the entities would stay available forever.
        """
        if self._grace_timer is not None or self._last_success is None:
            return
        remaining = STALE_DATA_GRACE - (dt_util.utcnow() - self._last_success)
        self._grace_timer = async_call_later(
            self.hass,
            max(remaining.total_seconds(), 0) + 1,
            self._async_grace_expired,
        )

    @callback
    def _async_grace_expired(self, _now: datetime) -> None:
        """Mark entities unavailable if refreshes are still failing."""
        self._grace_timer = None
        if not self.last_update_success:
            self.async_update_listeners()

    @callback
    def _cancel_grace_timer(self) -> None:
        if self._grace_timer is not None:
            self._grace_timer()
            self._grace_timer = None

    async def async_shutdown(self) -> None:
        """Cancel the grace timer."""
        self._cancel_grace_timer()
        await super().async_shutdown()
