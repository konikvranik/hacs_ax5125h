"""DataUpdateCoordinator pro soundbar AX5125H."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .client import EzplayClient, EzplayError
from .const import (
    CONF_BUS_LOG_SWITCH,
    CONF_POLL_INTERVAL,
    CONF_POWER_ENTITY,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_POLL_INTERVAL,
)
from .transports import TransportError

_LOGGER = logging.getLogger(__name__)

#: Po zapnutí bus logu potřebuje gateway chvíli, než začne publikovat rx.
BUS_LOG_WARMUP = 1.0


class SoundbarCoordinator(DataUpdateCoordinator[dict]):
    """Pollí soundbar a zveřejňuje data; push (A5) jde přes on_update."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 client: EzplayClient) -> None:
        interval = max(
            MIN_POLL_INTERVAL,
            int(entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.entry = entry
        self.client = client
        self._bus_log_entity = entry.data.get(CONF_BUS_LOG_SWITCH) or None
        self._power_entity = entry.data.get(CONF_POWER_ENTITY) or None
        self._bus_log_ever_on = False

    async def _async_update_data(self) -> dict:
        try:
            if not self.client.rx_capable:
                self._refresh_power_from_entity()
                self.client.data["last_seen"] = time.time()
                return self.client.data
            await self._ensure_bus_log()
            return await self.client.async_refresh()
        except (EzplayError, TransportError) as exc:
            raise UpdateFailed(str(exc)) from exc

    async def async_power_on(self) -> None:
        """Zapne soundbar přes transport a případnou power entity."""
        if self._power_entity and not self.client.rx_capable:
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": self._power_entity}
            )
        await self.client.async_power_on()
        self.async_update_listeners()

    async def async_power_off(self) -> None:
        """Vypne soundbar přes transport a případnou power entity."""
        if self._power_entity and not self.client.rx_capable:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self._power_entity}
            )
        await self.client.async_power_off()
        self.async_update_listeners()

    def _refresh_power_from_entity(self) -> None:
        """rx-less transport: power z hdmi_cec entity (switch.hdmi_N)."""
        if not self._power_entity:
            return
        state = self.hass.states.get(self._power_entity)
        if state is None:
            return
        if state.state == "on":
            self.client.data["power"] = "on"
        elif state.state == "off":
            self.client.data["power"] = "standby"

    async def _ensure_bus_log(self) -> None:
        """Gateway publikuje rx jen se zapnutým "CEC Bus Log Enabled"."""
        if not self._bus_log_entity:
            return
        state = self.hass.states.get(self._bus_log_entity)
        if state is None:
            return  # entita zmizela — neblokujeme
        if state.state == "on":
            self._bus_log_ever_on = True
            return
        _LOGGER.info("zapínám CEC bus log (%s)", self._bus_log_entity)
        await self.hass.services.async_call(
            "switch", "turn_on", {"entity_id": self._bus_log_entity}
        )
        if not self._bus_log_ever_on:
            self._bus_log_ever_on = True
            await asyncio.sleep(BUS_LOG_WARMUP)
