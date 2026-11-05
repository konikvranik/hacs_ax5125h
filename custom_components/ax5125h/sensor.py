"""Sensory: diagnostika (support features, active source). Hlasitost je
od 0.4.2 number slider (VolumeNumber)."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
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
            SupportFeaturesSensor(coordinator),
            ActiveSourceSensor(coordinator),
        ]
    )


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
