"""Přepínače soundbaru: napájení a mute."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .client import EzplayError
from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .entity import AxEntity
from .transports import TransportError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SoundbarCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            PowerSwitch(coordinator),
            MuteSwitch(coordinator),
        ]
    )


class PowerSwitch(AxEntity, SwitchEntity):
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_translation_key = "power"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, translation_key="power")

    @property
    def is_on(self) -> bool | None:
        power = self.sb_data.get("power")
        if power is None or power == "unknown":
            return None
        return power == "on"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        power = self.sb_data.get("power")
        if power:
            attrs["power_status"] = power
        # raw CEC Report Power Status — na ne-CEC vstupech (OPT/AUX/USB/BT)
        # zůstává standby, i když soundbar hraje (proto power_status = on)
        cec = self.sb_data.get("cec_power")
        if cec:
            attrs["cec_power_status"] = cec
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.async_power_on()
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.async_power_off()
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc


class MuteSwitch(AxEntity, SwitchEntity, RestoreEntity):
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_translation_key = "mute"
    _attr_icon = "mdi:volume-mute"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, translation_key="mute")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sb_data.get("mute") is None:
            last_state = await self.async_get_last_state()
            if last_state and last_state.state in ("on", "off"):
                self.coordinator.data.setdefault("mute", last_state.state == "on")

    @property
    def is_on(self) -> bool | None:
        mute = self.sb_data.get("mute")
        return None if mute is None else bool(mute)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_mute(True)
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_mute(False)
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()
