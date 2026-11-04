"""Config flow: volba transportu + parametry + živá validace handshake."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .client import EzplayClient, EzplayTimeout
from .const import (
    CONF_ADAPTER,
    CONF_BUS_LOG_SWITCH,
    CONF_GW_ADDR,
    CONF_HDMI_PA,
    CONF_HOST,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_PREFIX,
    CONF_SB_ADDR,
    CONF_SERVICE_BASE,
    CONF_TRANSPORT,
    CONF_TV_ADDR,
    DEFAULT_GW_ADDR,
    DEFAULT_HDMI_PA,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_PREFIX,
    DEFAULT_SB_ADDR,
    DEFAULT_TV_ADDR,
    DOMAIN,
    MIN_POLL_INTERVAL,
    TITLE,
    TRANSPORTS,
    TRANSPORT_ESPHOME,
    TRANSPORT_ESPHOME_MQTT,
    TRANSPORT_LIBCEC,
    TRANSPORT_PYCEC_TCP,
)
from .ezplay import EQ_OPTIONS, pa_from_str
from .transports import TransportError, build_transport

_LOGGER = logging.getLogger(__name__)

TRANSPORT_LABELS = {
    TRANSPORT_ESPHOME_MQTT: "ESPHome HDMI-CEC gateway (MQTT) — doporučeno",
    TRANSPORT_ESPHOME: "ESPHome HDMI-CEC gateway (native API)",
    TRANSPORT_PYCEC_TCP: "pyCEC TCP bridge (libcec remote)",
    TRANSPORT_LIBCEC: "libcec (lokální adapter)",
}

STEP_ESPHOME_MQTT = vol.Schema(
    {
        vol.Required(CONF_PREFIX, default=DEFAULT_PREFIX): str,
        vol.Optional(CONF_BUS_LOG_SWITCH, description="bus_log_suggestion"): str,
    }
)
STEP_ESPHOME = vol.Schema(
    {
        vol.Required(CONF_SERVICE_BASE): str,
        vol.Optional(CONF_BUS_LOG_SWITCH, description="bus_log_suggestion"): str,
    }
)
STEP_PYCEC_TCP = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)
STEP_LIBCEC = vol.Schema({vol.Optional(CONF_ADAPTER, default=""): str})

STEP_COMMON = vol.Schema(
    {
        vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): int,
        vol.Required(CONF_HDMI_PA, default=DEFAULT_HDMI_PA): str,
        vol.Optional(CONF_TV_ADDR, default=DEFAULT_TV_ADDR): int,
        vol.Optional(CONF_SB_ADDR, default=DEFAULT_SB_ADDR): int,
        vol.Optional(CONF_GW_ADDR, default=DEFAULT_GW_ADDR): int,
    }
)


def _esphome_service_bases(hass: HomeAssistant) -> list[str]:
    services = hass.services.async_services().get("esphome", {})
    bases = set()
    for name in services:
        if name.endswith("_cec_send_src"):
            bases.add(name[: -len("_cec_send_src")])
        elif name.endswith("_cec_send"):
            bases.add(name[: -len("_cec_send")])
    return sorted(bases)


def _bus_log_switches(hass: HomeAssistant) -> list[str]:
    found = []
    for state in hass.states.async_all("switch"):
        if "cec_bus_log" in state.entity_id or state.name == "CEC Bus Log Enabled":
            found.append(state.entity_id)
    return sorted(found)


async def _async_validate(hass: HomeAssistant, data: dict) -> tuple[dict, str | None]:
    """Živý handshake přes vybraný transport. Vrátí (data, error_key)."""
    try:
        transport = build_transport(hass, data)
    except (KeyError, ValueError):
        return {}, "invalid_transport"
    client = EzplayClient(
        transport,
        tv_addr=data[CONF_TV_ADDR],
        sb_addr=data[CONF_SB_ADDR],
        gw_addr=data[CONF_GW_ADDR],
        hdmi_pa=pa_from_str(data[CONF_HDMI_PA]),
    )
    try:
        await client.async_start()
        # zapnout bus log přes switch entity, pokud je nastavená
        bus_log = data.get(CONF_BUS_LOG_SWITCH)
        if bus_log and hass.states.get(bus_log) is not None \
                and hass.states.get(bus_log).state != "on":
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": bus_log}
            )
            await asyncio.sleep(1.0)
        try:
            state = await asyncio.wait_for(client.async_refresh(), timeout=12.0)
        finally:
            await client.async_stop()
    except TransportError as exc:
        _LOGGER.debug("transport selhal: %s", exc)
        return {}, "transport_error"
    except (asyncio.TimeoutError, EzplayTimeout, OSError):
        return {}, "no_response"
    except Exception:  # noqa: BLE001
        _LOGGER.exception("validace selhala")
        return {}, "transport_error"
    return state, None


class Ax5125hConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            transport = user_input[CONF_TRANSPORT]
            if transport == TRANSPORT_ESPHOME_MQTT:
                return await self.async_step_esphome_mqtt()
            if transport == TRANSPORT_ESPHOME:
                return await self.async_step_esphome()
            if transport == TRANSPORT_PYCEC_TCP:
                return await self.async_step_pycec_tcp()
            return await self.async_step_libcec()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRANSPORT): vol.In(
                        {k: TRANSPORT_LABELS[k] for k in TRANSPORTS}
                    )
                }
            ),
        )

    async def async_step_esphome_mqtt(self, user_input=None) -> FlowResult:
        errors: dict = {}
        if user_input is not None:
            try:
                from homeassistant.components import mqtt  # noqa: PLC0415

                mqtt_ready = await mqtt.async_wait_for_mqtt_client(self.hass)
            except ImportError:
                mqtt_ready = False
            if not mqtt_ready:
                errors["base"] = "mqtt_missing"
            else:
                if user_input.get(CONF_BUS_LOG_SWITCH):
                    user_input[CONF_BUS_LOG_SWITCH] = user_input[
                        CONF_BUS_LOG_SWITCH].strip()
                self._data.update(user_input)
                return await self.async_step_common()
        kwargs = {}
        switches = _bus_log_switches(self.hass)
        if switches:
            kwargs[CONF_BUS_LOG_SWITCH] = vol.Optional(
                CONF_BUS_LOG_SWITCH, description="bus_log_suggestion",
                default=switches[0],
            )
        schema = vol.Schema({vol.Required(CONF_PREFIX, default=DEFAULT_PREFIX): str})
        if kwargs:
            schema = schema.extend(kwargs)
        return self.async_show_form(
            step_id="esphome_mqtt", data_schema=schema, errors=errors
        )

    async def async_step_esphome(self, user_input=None) -> FlowResult:
        errors: dict = {}
        bases = _esphome_service_bases(self.hass)
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_common()
        if bases:
            schema = vol.Schema(
                {vol.Required(CONF_SERVICE_BASE, default=bases[0]): vol.In(bases)}
            )
        else:
            schema = vol.Schema({vol.Required(CONF_SERVICE_BASE): str})
            errors["base"] = "esphome_services_missing"
        switches = _bus_log_switches(self.hass)
        if switches:
            schema = schema.extend(
                {vol.Optional(CONF_BUS_LOG_SWITCH, default=switches[0]): str}
            )
        return self.async_show_form(
            step_id="esphome", data_schema=schema, errors=errors
        )

    async def async_step_pycec_tcp(self, user_input=None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_common()
        return self.async_show_form(step_id="pycec_tcp", data_schema=STEP_PYCEC_TCP)

    async def async_step_libcec(self, user_input=None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_common()
        return self.async_show_form(step_id="libcec", data_schema=STEP_LIBCEC)

    async def async_step_common(self, user_input=None) -> FlowResult:
        errors: dict = {}
        if user_input is not None:
            self._data.update(user_input)
            if self._data[CONF_POLL_INTERVAL] < MIN_POLL_INTERVAL:
                errors[CONF_POLL_INTERVAL] = "poll_too_fast"
            else:
                try:
                    pa_from_str(self._data[CONF_HDMI_PA])
                except ValueError:
                    errors[CONF_HDMI_PA] = "bad_pa"
            if not errors:
                await self.async_set_unique_id(
                    f"{self._data[CONF_TRANSPORT]}"
                    f"-{self._data.get(CONF_HOST) or self._data.get(CONF_PREFIX) or ''}"
                    f"-{self._data[CONF_SB_ADDR]}"
                )
                self._abort_if_unique_id_configured()
                state, error = await _async_validate(self.hass, self._data)
                if error:
                    errors["base"] = error
                else:
                    eq = state.get("eq")
                    desc_placeholders = {
                        "eq": EQ_OPTIONS[eq] if eq is not None and eq < len(EQ_OPTIONS) else "?",
                        "power": state.get("power", "?"),
                    }
                    return self.async_create_entry(
                        title=TITLE,
                        data=self._data,
                        description_placeholders=desc_placeholders,
                    )
        return self.async_show_form(
            step_id="common", data_schema=STEP_COMMON, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> Ax5125hOptionsFlow:
        return Ax5125hOptionsFlow(config_entry)


class Ax5125hOptionsFlow(config_entries.OptionsFlow):
    """Změna adres, PA, poll intervalu a bus log switchi bez nové validace."""

    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        super().__init__()
        self._entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        entry = self._entry or getattr(self, "config_entry", None)
        if entry is None:
            return self.async_abort(reason="no_entry")
        if user_input is not None:
            data = dict(entry.data)
            data.update(user_input)
            self.hass.config_entries.async_update_entry(entry, data=data)
            return self.async_create_entry(title="", data={})
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): int,
                vol.Required(
                    CONF_HDMI_PA, default=entry.data.get(CONF_HDMI_PA, DEFAULT_HDMI_PA)
                ): str,
                vol.Optional(
                    CONF_TV_ADDR, default=entry.data.get(CONF_TV_ADDR, DEFAULT_TV_ADDR)
                ): int,
                vol.Optional(
                    CONF_SB_ADDR, default=entry.data.get(CONF_SB_ADDR, DEFAULT_SB_ADDR)
                ): int,
                vol.Optional(
                    CONF_GW_ADDR, default=entry.data.get(CONF_GW_ADDR, DEFAULT_GW_ADDR)
                ): int,
                vol.Optional(
                    CONF_BUS_LOG_SWITCH,
                    default=entry.data.get(CONF_BUS_LOG_SWITCH, ""),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
