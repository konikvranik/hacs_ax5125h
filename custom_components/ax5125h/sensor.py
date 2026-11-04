"""Sensory: napájení, hlasitost + mute, diagnostika (support features,
active source)."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
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

POWER_OPTIONS = ["on", "standby", "turning_on", "turning_off", "unknown"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SoundbarCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            PowerSensor(coordinator),
            VolumeSensor(coordinator),
            SupportFeaturesSensor(coordinator),
            ActiveSourceSensor(coordinator),
        ]
    )


class PowerSensor(AxEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = POWER_OPTIONS
    _attr_translation_key = "power"

    @property
    def native_value(self) -> StateType:
        return self.sb_data.get("power")


class VolumeSensor(AxEntity, SensorEntity):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "volume"

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

    @property
    def native_value(self) -> StateType:
        return self.sb_data.get("active_source")
