"""Select entity: EQ, Surround (tri-state přes RTVR), Dimmer, vstup."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .client import EzplayError
from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .entity import AxEntity
from .ezplay import (
    EQ_OPTIONS,
    ITEM_EQ,
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
    """Surround tri-state PURE/PRO/OFF přes RTVR (opt1: 0=PURE, 1=PRO,
    2=OFF) — EZPlay item2 je jen binární flag (0=off, 1=on, mód nezná).
    RTVR poll běží v každém refreshi; handshake jen 1× / 5 min pokus.
    IR surround tlačítko: off inform → OFF; on inform → mód doplní poll."""

    _attr_icon = "mdi:surround-sound"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "surround", ("PURE", "PRO", "OFF"))

    @property
    def current_option(self) -> str | None:
        value = self.sb_data.get("surr")
        return ("PURE", "PRO", "OFF")[value] if value in (0, 1, 2) else None

    async def async_select_option(self, option: str) -> None:
        mode = {"PURE": 0, "PRO": 1, "OFF": 2}[option]
        try:
            ok = await self.coordinator.client.rtvr_set_surr(mode)
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        if not ok:
            raise HomeAssistantError(
                f"surround mód {option} se nepodařilo zapsat přes RTVR "
                "(session nedorazila) — zkus znovu / reload integrace"
            )
        self.coordinator.async_update_listeners()


class DimmerSelect(_BaseSelect):
    """Jas displeje: menu 0–3, A2 SET raw menu hodnotou.

    Wire (A1/A5) clampne na 0–2 — po zápisu 3 čte zpětně 2 (limit FW
    builderu, displej 3 ukáže). Auto-dim displeje = 0.
    """

    _attr_icon = "mdi:brightness-6"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "dimmer", ("0", "1", "2", "3"))

    @property
    def current_option(self) -> str | None:
        value = self.sb_data.get("dimmer")
        if value is None:
            return None
        return str(min(value, 3))

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.async_set_dimmer(int(option))
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()


class InputSelect(_BaseSelect, RestoreEntity):
    """Vstup: ARC (SAM Request za TV) / HDMI IN (Active Source za gateway)
    se přepínají CEC; OPT/AUX/USB/BT CEC nepřepne ani nevidí — výběr v HA
    je DEKLARACE stavu (přepni IR, HA stav ukazuje; CEC kotvy a
    source_pressed ho korigují).

    Aktuální stav je odvozený z CEC kotev + počítání IR SOURCE stisků +
    optimisticky po příkazu (soundbar svůj vstup sám nehlásí).
    """

    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, coordinator: SoundbarCoordinator) -> None:
        super().__init__(coordinator, "input", tuple(INPUT_LABELS.values()))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sb_data.get("input") is None:
            last = await self.async_get_last_state()
            if last is not None and last.state in _LABEL_TO_INPUT:
                self.coordinator.client.data["input"] = \
                    _LABEL_TO_INPUT[last.state]

    @property
    def current_option(self) -> str | None:
        return INPUT_LABELS.get(self.sb_data.get("input"))

    async def async_select_option(self, option: str) -> None:
        key = _LABEL_TO_INPUT.get(option)
        if key is None:
            return
        if key not in ("arc", "hdmi"):
            # OPT/AUX/USB/BT: CEC je neumí přepnout (soundbar na ně
            # nereaguje žádným rámcem) — výběr = synchronizace stavu.
            _LOGGER.info(
                "vstup %s deklarován (CEC ho nepřepne — přepni IR a "
                "nech automaticky korigovat kotvami/source_pressed)", option,
            )
            self.coordinator.client.data["input"] = key
            self.coordinator.async_update_listeners()
            return
        try:
            await self.coordinator.client.async_set_input(key)
        except (EzplayError, TransportError, ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self.coordinator.async_update_listeners()
