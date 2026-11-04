"""Select entity: EQ, Surround, Dimmer, vstup."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import EzplayError
from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .entity import AxEntity
from .ezplay import (
    DIMMER_INTERNAL_MAX,
    DIMMER_INTERNAL_MIN,
    EQ_OPTIONS,
    ITEM_DIMMER,
    ITEM_EQ,
    ITEM_SURROUND,
    SURROUND_OPTIONS,
)
from .transports import TransportError

_LOGGER = logging.getLogger(__name__)

INPUT_OPTIONS = ("ARC", "HDMI")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SoundbarCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            EqSelect(coordinator),
            SurroundSelect(coordinator),
            DimmerSelect(coordinator),
            InputSelect(coordinator),
        ]
    )


class _BaseSelect(AxEntity, SelectEntity):
    def __init__(self, coordinator: SoundbarCoordinator, key: str,
                 options: tuple[str, ...]) -> None:
        super().__init__(coordinator, translation_key=key)
        self._attr_options = list(options)


class EqSelect(_BaseSelect):
    _attr_icon = "mdi:equalizer"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "eq", EQ_OPTIONS)

    @property
    def current_option(self) -> str | None:
        value = self.sb_data.get("eq")
        if value is None or not 0 <= value < len(EQ_OPTIONS):
            return None
        return EQ_OPTIONS[value]

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.async_set_item(
                ITEM_EQ, EQ_OPTIONS.index(option)
            )
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()


class SurroundSelect(_BaseSelect):
    _attr_icon = "mdi:surround-sound"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "surround", SURROUND_OPTIONS)

    @property
    def current_option(self) -> str | None:
        value = self.sb_data.get("surr")
        if value is None or not 0 <= value < len(SURROUND_OPTIONS):
            return None
        return SURROUND_OPTIONS[value]

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.async_set_item(
                ITEM_SURROUND, SURROUND_OPTIONS.index(option)
            )
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()


class DimmerSelect(_BaseSelect):
    """Jas displeje: menu 0-3 = interně 1-4 (rozsah 5+ korumpuje menu!).

    A1 hlásí interní 0, když si soundbar displej po nečinnosti sám ztlumí —
    zobrazujeme jako nejslabší úroveň (zápis nikdy 0, vždy 1-4).
    """

    _attr_icon = "mdi:brightness-6"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(
            coordinator, "dimmer",
            tuple(str(i) for i in range(
                DIMMER_INTERNAL_MIN - 1, DIMMER_INTERNAL_MAX)),
        )

    @property
    def current_option(self) -> str | None:
        value = self.sb_data.get("dimmer")
        if value is None:
            return None
        if value <= 0:
            return "0"  # display auto-ztlumen (interní 0)
        if value > DIMMER_INTERNAL_MAX:
            return None
        return str(value - DIMMER_INTERNAL_MIN)

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.async_set_item(
                ITEM_DIMMER, int(option) + DIMMER_INTERNAL_MIN
            )
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()


class InputSelect(_BaseSelect):
    """Vstup: ARC (SAM Request za TV) / HDMI IN (Active Source za gateway).

    Aktuální stav je odvozený z Active Source broadcastů + optimisticky po
    příkazu (soundbar svůj vstup nehlásí).
    """

    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "input", INPUT_OPTIONS)

    @property
    def current_option(self) -> str | None:
        value = self.sb_data.get("input")
        return value.upper() if value in ("arc", "hdmi") else None

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.async_set_input(option.lower())
        except (EzplayError, TransportError, ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()
