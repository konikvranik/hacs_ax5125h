"""Media player entita — hlasový asistent (volume_set), napájení, zdroje.

Hlavní důvod: voice assistant (Assist) umí `volume_set` jen na media_player
entitě (set_level sentence). Zde mapuje na EZPlay Set_Volume (absolutní
0–100 %, živě ověřeno ve 0.4.2).
"""

from __future__ import annotations

import logging

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .client import EzplayError
from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .entity import AxEntity
from .ezplay import UCP_VOLUME_DOWN, UCP_VOLUME_UP
from .select import INPUT_LABELS
from .transports import TransportError

_LOGGER = logging.getLogger(__name__)

_LABEL_TO_INPUT = {v: k for k, v in INPUT_LABELS.items()}

_POWER_STATES = {
    "on": MediaPlayerState.ON,
    "turning_on": MediaPlayerState.ON,
    "standby": MediaPlayerState.STANDBY,
    "turning_off": MediaPlayerState.STANDBY,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SoundbarCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([SoundbarMediaPlayer(coordinator)])


class SoundbarMediaPlayer(AxEntity, MediaPlayerEntity, RestoreEntity):
    """Soundbar jako media player: hlasitost (absolutní + kroky + mute),
    napájení a výběr zdroje (ARC/HDMI CEC přepne, OPT/AUX/USB/BT = deklarace
    stejně jako InputSelect)."""

    _attr_translation_key = "soundbar"
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, translation_key="soundbar")
        self._attr_source_list = list(INPUT_LABELS.values())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sb_data.get("power") is None:
            last = await self.async_get_last_state()
            if last is not None:
                if last.state == MediaPlayerState.ON:
                    self.coordinator.client.data["power"] = "on"
                elif last.state in (MediaPlayerState.STANDBY, MediaPlayerState.OFF):
                    self.coordinator.client.data["power"] = "standby"

    @property
    def state(self) -> MediaPlayerState | None:
        return _POWER_STATES.get(self.sb_data.get("power"))

    @property
    def volume_level(self) -> float | None:
        value = self.sb_data.get("volume")
        if value is None:
            return None
        return max(0.0, min(1.0, int(value) / 100))

    @property
    def is_volume_muted(self) -> bool | None:
        mute = self.sb_data.get("mute")
        return None if mute is None else bool(mute)

    @property
    def source(self) -> str | None:
        return INPUT_LABELS.get(self.sb_data.get("input"))

    async def async_set_volume_level(self, volume: float) -> None:
        try:
            await self.coordinator.client.async_set_volume(round(volume * 100))
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()

    async def async_volume_up(self) -> None:
        await self._volume_key(UCP_VOLUME_UP)

    async def async_volume_down(self) -> None:
        await self._volume_key(UCP_VOLUME_DOWN)

    async def _volume_key(self, code: int) -> None:
        try:
            await self.coordinator.client.async_volume_key(code)
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()

    async def async_mute_volume(self, mute: bool) -> None:
        try:
            await self.coordinator.client.async_set_mute(mute)
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()

    async def async_turn_on(self) -> None:
        try:
            await self.coordinator.async_power_on()
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_turn_off(self) -> None:
        try:
            await self.coordinator.async_power_off()
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_select_source(self, source: str) -> None:
        key = _LABEL_TO_INPUT.get(source)
        if key is None:
            return
        if key not in ("arc", "hdmi"):
            # OPT/AUX/USB/BT: CEC je nepřepne — deklarace stavu (jako InputSelect)
            self.coordinator.client.data["input"] = key
            self.coordinator.async_update_listeners()
            return
        try:
            await self.coordinator.client.async_set_input(key)
        except (EzplayError, TransportError, ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()
