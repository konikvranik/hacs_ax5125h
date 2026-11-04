"""Společný základ entit soundbaru."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import SoundbarCoordinator


class AxEntity(CoordinatorEntity[SoundbarCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SoundbarCoordinator,
                 translation_key: str | None = None) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = translation_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Soundbar AX5125H",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def sb_data(self) -> dict:
        return self.coordinator.data or {}
