"""Number entity: BASS a TREBLE (-5..+5)."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import EzplayError
from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .entity import AxEntity
from .ezplay import (
    BASS_TREBLE_MAX,
    BASS_TREBLE_MIN,
    ITEM_BASS,
    ITEM_TREBLE,
    encode_bass_treble,
)
from .transports import TransportError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SoundbarCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [BassNumber(coordinator), TrebleNumber(coordinator)]
    )


class _AxNumber(AxEntity, NumberEntity):
    _attr_native_min_value = BASS_TREBLE_MIN
    _attr_native_max_value = BASS_TREBLE_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SoundbarCoordinator, key: str,
                 item: int) -> None:
        super().__init__(coordinator, translation_key=key)
        self._item = item

    @property
    def native_value(self) -> float | None:
        value = self.sb_data.get(self._data_key)
        if value is None:
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.async_set_item(
                self._item, encode_bass_treble(int(value))
            )
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()


class BassNumber(_AxNumber):
    _data_key = "bass"
    _attr_icon = "mdi:music-clef-bass"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "bass", ITEM_BASS)


class TrebleNumber(_AxNumber):
    _data_key = "treble"
    _attr_icon = "mdi:music-clef-treble"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "treble", ITEM_TREBLE)
