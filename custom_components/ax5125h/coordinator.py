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

#: Idle prah (s bez jakékoli aktivity/push) — po něm přejdeme do silent
#: módu: ŽÁDNÉ dotazy směrem k baru. Jakýkoliv CEC dotaz totiž baru brání
#: v auto-standby (neprojde „BYE" do hlubokého spánku, pozorování
#: uživatele: BYE → LED zhasne za sekundy, pokud nic dotazy neruší).
IDLE_AFTER = 600.0

#: Cadence silent módu — cyklus posílá NULA rámců (jen bookkeeping a
#: re-evaluace cadence); push zprávy stav aktualizují okamžitě i tak.
IDLE_POLL_INTERVAL = 900


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
        self._active_interval = interval
        self._traffic = True
        self._bus_log_entity = entry.data.get(CONF_BUS_LOG_SWITCH) or None
        self._power_entity = entry.data.get(CONF_POWER_ENTITY) or None
        self._bus_log_ever_on = False

    def handle_push(self, data: dict) -> None:
        """Callback pro push z klienta (0x90/0x82/0x7A/A5/...).

        Publikuje stav okamžitě a přepolluje cadenci: push = aktivita →
        aktivní okno (rychlý poll), standby událost → silent mód (nula
        dotazů, ať bar projde BYE do hlubokého spánku).
        """
        self.async_set_updated_data(data)
        self._adapt_polling(data)

    async def _async_update_data(self) -> dict:
        try:
            if not self.client.rx_capable:
                self._refresh_power_from_entity()
                self.client.data["last_seen"] = time.time()
                return self.client.data
            await self._ensure_bus_log()
            result = await self.client.async_refresh(traffic=self._traffic)
            self._adapt_polling(result)
            return result
        except (EzplayError, TransportError) as exc:
            raise UpdateFailed(str(exc)) from exc

    def _adapt_polling(self, data: dict) -> None:
        """Cadence dle stavu a aktivity:
        - standby NEBO neaktivní (> IDLE_AFTER bez příkazu/pushu) = silent
          mód (IDLE_POLL_INTERVAL, traffic=False) — nula dotazů, ať bar
          projde BYE do hlubokého spánku;
        - aktivní (on + poslední příkaz/push < IDLE_AFTER) = konfigurovaný
          interval s plným refreshem.
        Push zprávy (0x7A/A5/72:01/…) a HA příkazy aktivitu obnovují
        okamžitě (client nastavuje last_activity), takže rychlý mód se
        vrací bez čekání.
        """
        standby = data.get("power") == "standby"
        active = (
            not standby
            and time.time() - data.get("last_activity", 0.0) < IDLE_AFTER
        )
        self._traffic = active
        target = timedelta(
            seconds=self._active_interval if active else IDLE_POLL_INTERVAL
        )
        if self.update_interval != target:
            self.update_interval = target
            _LOGGER.debug(
                "poll cadence → %s (%s)",
                target,
                "aktivní" if active else "silent (nula dotazů)",
            )

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
