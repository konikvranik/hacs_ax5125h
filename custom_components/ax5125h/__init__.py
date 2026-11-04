"""Hisense AX5125H soundbar přes HDMI-CEC (EZPlay vendor protokol).

Integrace registruje soundbar přes jeden z transportů (ESPHome CEC gateway
MQTT/native, pyCEC TCP bridge, lokální libcec) a zveřejňuje EQ, bass, treble,
surround, dimmer, napájení, hlasitost a přepínání vstupů ARC/HDMI IN.
"""

from __future__ import annotations

import logging
from functools import partial

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .client import EzplayClient, EzplayError
from .const import DOMAIN
from .coordinator import SoundbarCoordinator
from .ezplay import (
    OP_EZPLAY,
    UCP_MUTE_TOGGLE,
    UCP_VOLUME_DOWN,
    UCP_VOLUME_UP,
    blocked_ezplay_sub,
    pa_from_str,
)
from .transports import TransportError, build_transport

_LOGGER = logging.getLogger(__name__)

PLATFORMS = (
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BUTTON,
)

DATA_CLIENT = "client"
DATA_COORDINATOR = "coordinator"

SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required("source"): vol.All(vol.Coerce(int), vol.Range(0, 15)),
        vol.Required("destination"): vol.All(vol.Coerce(int), vol.Range(0, 15)),
        vol.Required("data"): cv.string,
    }
)

TARGET_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})


def _parse_data_field(text: str) -> list[int]:
    """'0xA0,0xA8,0x82,0x00,0xA0' (nebo mezery) -> bajty; int(x, 0)."""
    tokens = [t for t in text.replace(" ", ",").split(",") if t]
    try:
        return [int(t, 0) & 0xFF for t in tokens]
    except ValueError as exc:
        raise ServiceValidationError(
            f"nelze naparsovat data: {text!r} (použijte 0xNN,0xNN nebo dec)"
        ) from exc


def _entry_of_entity(hass: HomeAssistant, entity_id: str) -> ConfigEntry | None:
    registry = er.async_get(hass)
    entity = registry.async_get(entity_id)
    if entity is None:
        return None
    return hass.config_entries.async_get_entry(entity.config_entry_id)


def _client_of_call(hass: HomeAssistant, call: ServiceCall) -> tuple[ConfigEntry, EzplayClient]:
    entity_id = call.data[ATTR_ENTITY_ID][0]
    entry = _entry_of_entity(hass, entity_id)
    if entry is None or entry.domain != DOMAIN or entry.entry_id not in hass.data[DOMAIN]:
        raise ServiceValidationError(
            f"{entity_id} nepatří integraci {DOMAIN}"
        )
    return entry, hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]


async def async_setup(hass: HomeAssistant, config) -> None:
    """Doménové služby (vytvoří se jednou)."""

    async def handle_send_raw(call: ServiceCall) -> ServiceResponse:
        entry = None
        if ATTR_ENTITY_ID in call.data:
            entry, _ = _client_of_call(hass, call)
        else:
            entries = hass.config_entries.async_entries(DOMAIN)
            if len(entries) != 1:
                raise ServiceValidationError(
                    "uveďte entity_id (nebo mějte právě jednu konfiguraci)"
                )
            entry = entries[0]
        client: EzplayClient = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]
        payload = _parse_data_field(call.data["data"])
        blocked = blocked_ezplay_sub(payload)
        if blocked is not None:
            raise ServiceValidationError(
                f"subopcode 0x{blocked:02X} je na blocklistu "
                "(destruktivní na AX5125H) — odmítnuto"
            )
        if payload and payload[0] == OP_EZPLAY and len(payload) == 6 \
                and payload[4] == 0xA2:
            raise ServiceValidationError(
                "A2 bez hodnoty není GET — zapíše garbage; pro čtení A3"
            )
        try:
            await client.async_send_raw(
                call.data["source"], call.data["destination"], payload
            )
        except (EzplayError, TransportError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        return {"result": "sent"}

    async def _volume_key(code: int, call: ServiceCall) -> None:
        _, client = _client_of_call(hass, call)
        await client.async_volume_key(code)

    hass.services.async_register(
        DOMAIN, "send_raw", handle_send_raw, schema=SEND_RAW_SCHEMA.extend(
            {vol.Optional(ATTR_ENTITY_ID): cv.entity_ids}
        ),
    )
    hass.services.async_register(
        DOMAIN, "volume_up", partial(_volume_key, UCP_VOLUME_UP),
        schema=TARGET_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "volume_down", partial(_volume_key, UCP_VOLUME_DOWN),
        schema=TARGET_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "mute_toggle", partial(_volume_key, UCP_MUTE_TOGGLE),
        schema=TARGET_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    transport = build_transport(hass, entry.data)
    client = EzplayClient(
        transport,
        tv_addr=int(entry.data.get("tv_addr", 0)),
        sb_addr=int(entry.data.get("sb_addr", 5)),
        gw_addr=int(entry.data.get("gw_addr", 14)),
        hdmi_pa=pa_from_str(entry.data.get("hdmi_pa", "3.1.0.0")),
    )
    coordinator = SoundbarCoordinator(hass, entry, client)

    # push notifikace (A5/Active Source) -> coordinator
    client.set_update_callback(coordinator.async_set_updated_data)

    try:
        await client.async_start()
        await coordinator.async_config_entry_first_refresh()
    except TransportError as exc:
        await client.async_stop()
        raise HomeAssistantError(f"transport není připraven: {exc}") from exc
    except Exception:
        await client.async_stop()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
    }
    await hass.config_entries.async_forward_platforms(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    store = hass.data[DOMAIN].pop(entry.entry_id, None)
    if store is not None:
        await store[DATA_CLIENT].async_stop()
    return unload_ok
