"""Number entity: BASS/TREBLE (−5..+5), jas displeje (0–3), hlasitost (0–100)."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

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
        [
            BassNumber(coordinator),
            TrebleNumber(coordinator),
            DimmerNumber(coordinator),
            VolumeNumber(coordinator),
        ]
    )


class _AxNumber(AxEntity, NumberEntity):
    _attr_native_min_value = BASS_TREBLE_MIN
    _attr_native_max_value = BASS_TREBLE_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

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


class DimmerNumber(AxEntity, NumberEntity):
    """Jas displeje 0–3 jako slider (od 0.4.1; dřív select).

    A2 SET raw menu hodnotou; wire (A1/A5) clampne na 0–2 — po zápisu 3
    čte zpětně 2 (limit FW builderu, displej 3 ukáže). Auto-dim displeje = 0.
    """

    _attr_native_min_value = 0
    _attr_native_max_value = 3
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:brightness-6"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, translation_key="dimmer")

    @property
    def native_value(self) -> float | None:
        value = self.sb_data.get("dimmer")
        if value is None:
            return None
        return float(min(value, 3))

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.async_set_dimmer(int(value))
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()


class VolumeNumber(AxEntity, RestoreEntity, NumberEntity):
    """Hlasitost 0–100 jako slider (od 0.4.2; dřív sensor).

    Zápis = absolutní EZPlay Set_Volume (živě ověřeno, push 0x7A hned
    potvrdí); čtení sleduje i IR/UCP změny přes coordinator data.
    Hodnota přežije restart (RestoreEntity, jako dřív sensor).
    """

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sb_data.get("volume") is None:
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state not in (
                "unknown", "unavailable", None,
            ):
                try:
                    self.coordinator.data.setdefault(
                        "volume", int(float(last_state.state)))
                except (TypeError, ValueError):
                    pass

    @property
    def native_value(self) -> float | None:
        value = self.sb_data.get("volume")
        if value is None:
            return None
        return float(min(100, max(0, int(value))))

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.async_set_volume(int(value))
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()
