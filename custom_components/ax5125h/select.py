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

#: Vstup select: klíč -> label. OPT/AUX/USB/BT CEC nevysílá nic — stav se
#: drží počítáním IR SOURCE stisků (service source_pressed) a CEC kotvy
#: (SAM On, Routing Change/Active Source) ho korigují.
INPUT_LABELS = {
    "arc": "ARC",
    "hdmi": "HDMI",
    "opt": "OPTICAL",
    "aux": "AUX",
    "usb": "USB",
    "bt": "BT",
}
_LABEL_TO_INPUT = {v: k for k, v in INPUT_LABELS.items()}


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
    """Vstup: ARC (SAM Request za TV) / HDMI IN (Active Source za gateway)
    se přepínají CEC; OPT/AUX/USB/BT jen sledujeme (počítání SOURCE stisků).

    Aktuální stav je odvozený z CEC kotev + počítání IR SOURCE stisků +
    optimisticky po příkazu (soundbar svůj vstup sám nehlásí).
    """

    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "input", tuple(INPUT_LABELS.values()))

    @property
    def current_option(self) -> str | None:
        return INPUT_LABELS.get(self.sb_data.get("input"))

    async def async_select_option(self, option: str) -> None:
        key = _LABEL_TO_INPUT.get(option)
        if key is None:
            return
        if key not in ("arc", "hdmi"):
            raise HomeAssistantError(
                f"vstup {option} nelze přes CEC přepnout — soundbar na něj "
                "nereaguje žádným rámcem. Přepni IR ovladačem (nebo IR "
                "blasterem) a zavolej service ax5125h.source_pressed, aby "
                "integrace stav sledovala."
            )
        try:
            await self.coordinator.client.async_set_input(key)
        except (EzplayError, TransportError, ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()
