"""Sensory: hlasitost, diagnostika (support features, active source)."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .entity import AxEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SoundbarCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            VolumeSensor(coordinator),
            SupportFeaturesSensor(coordinator),
            ActiveSourceSensor(coordinator),
        ]
    )


class VolumeSensor(AxEntity, RestoreSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sb_data.get("volume") is None:
            last_state = await self.async_get_last_sensor_data()
            if last_state and last_state.native_value is not None:
                self.coordinator.data.setdefault("volume", int(last_state.native_value))

    @property
    def native_value(self) -> StateType:
        volume = self.sb_data.get("volume")
        return None if volume is None else int(volume)

    @property
    def extra_state_attributes(self) -> dict:
        mute = self.sb_data.get("mute")
        return {"muted": mute} if mute is not None else {}


class SupportFeaturesSensor(AxEntity, SensorEntity):
    """0x91 report: maska podporovaných skupin + počet itemů (diagnostika)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "support_features"
    _attr_icon = "mdi:feature-search"

    @property
    def native_value(self) -> StateType:
        mask = self.sb_data.get("support_mask")
        return None if mask is None else f"0x{mask:02X}"

    @property
    def extra_state_attributes(self) -> dict:
        items = self.sb_data.get("support_items")
        return {"items": items} if items is not None else {}


class ActiveSourceSensor(AxEntity, SensorEntity):
    """Poslední viděný Active Source broadcast na CEC sběrnici."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "active_source"
    _attr_icon = "mdi:cast-audio"

    @property
    def native_value(self) -> StateType:
        return self.sb_data.get("active_source")
