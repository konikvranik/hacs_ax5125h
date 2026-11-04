"""Tlačítka hlasitosti (CEC User Control Pressed/Released)."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import EzplayError
from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .entity import AxEntity
from .ezplay import UCP_MUTE_TOGGLE, UCP_VOLUME_DOWN, UCP_VOLUME_UP
from .transports import TransportError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SoundbarCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            VolumeUpButton(coordinator),
            VolumeDownButton(coordinator),
            MuteButton(coordinator),
        ]
    )


class _VolumeButton(AxEntity, ButtonEntity):
    def __init__(self, coordinator: SoundbarCoordinator, key: str,
                 code: int) -> None:
        super().__init__(coordinator, translation_key=key)
        self._code = code

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.async_volume_key(self._code)
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc


class VolumeUpButton(_VolumeButton):
    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "volume_up", UCP_VOLUME_UP)


class VolumeDownButton(_VolumeButton):
    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "volume_down", UCP_VOLUME_DOWN)


class MuteButton(_VolumeButton):
    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "mute", UCP_MUTE_TOGGLE)
