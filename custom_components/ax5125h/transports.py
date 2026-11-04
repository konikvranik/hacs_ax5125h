"""CEC transporty: ESPHome gateway (MQTT / native API), HA hdmi_cec integrace.

Všechny transporty jsou zdrojově transparentní — EZPlay vyžaduje odesílatele
TV (adresa 0) a přepnutí na HDMI IN naopak gateway (0xE). MQTT, ESPHome
cec_send_src i hdmi_cec.send_command raw to umí vždy.
"""

from __future__ import annotations

import logging
from typing import Callable

from .ezplay import (
    encode_gateway_message,
    encode_pycec_message,
    frame_from_text,
)

_LOGGER = logging.getLogger(__name__)

FrameCallback = Callable[[int, int, list[int]], None]


class TransportError(Exception):
    """Transport není dostupný / neumí požadovanou operaci."""


class CecTransport:
    """Bázová třída — implementuje async_send + doručování rámců."""

    #: Umí transport doručovat přijaté rámce (rx)? hdmi_cec integrace rx
    #: eventy nevysílá → False (vendor stavy pak jen optimistic).
    supports_rx = True

    def __init__(self) -> None:
        self._on_frame: FrameCallback | None = None

    def set_frame_callback(self, callback: FrameCallback) -> None:
        self._on_frame = callback

    def _deliver(self, src: int, dst: int, payload: list[int]) -> None:
        if self._on_frame is not None:
            self._on_frame(src, dst, payload)

    async def async_start(self, callback: FrameCallback) -> None:
        self.set_frame_callback(callback)

    async def async_stop(self) -> None:
        self._on_frame = None

    async def async_send(self, src: int, dst: int, payload: list[int]) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ESPHome HDMI-CEC gateway přes MQTT (doporučená cesta)
# ---------------------------------------------------------------------------
class MqttCecTransport(CecTransport):
    """ESPHome hdmi-cec-gateway: publish ``<prefix>/send``, subscribe ``<prefix>/rx``.

    POZOR: rx se publikuje jen při zapnutém "CEC Bus Log Enabled" na gatewayi
    (integrace ho umí automaticky zapnout — viz bus_log_switch).
    """

    def __init__(self, hass, prefix: str) -> None:
        super().__init__()
        self._hass = hass
        self._prefix = prefix.rstrip("/")
        self._unsub = None

    async def async_start(self, callback: FrameCallback) -> None:
        from homeassistant.components import mqtt  # noqa: PLC0415

        await super().async_start(callback)
        self._unsub = await mqtt.async_subscribe(
            self._hass, f"{self._prefix}/rx", self._on_message
        )

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_stop()

    def _on_message(self, msg) -> None:
        text = msg.payload.decode("utf-8", errors="replace")
        frame = frame_from_text(text)
        if frame is not None:
            self._deliver(*frame)

    async def async_send(self, src: int, dst: int, payload: list[int]) -> None:
        from homeassistant.components import mqtt  # noqa: PLC0415

        await mqtt.async_publish(
            self._hass,
            f"{self._prefix}/send",
            encode_gateway_message(src, dst, payload),
        )


# ---------------------------------------------------------------------------
# ESPHome native API (akce cec_send / cec_send_src + event esphome.cec_rx)
# ---------------------------------------------------------------------------
class EspHomeCecTransport(CecTransport):
    """ESPHome hdmi-cec-gateway přes native API.

    - odeslání: služba ``esphome.<base>_cec_send_src`` (source/destination/
      data; vyžaduje gateway s touto akcí) s fallbackem na ``_cec_send``
      (odesílatel vždy gateway 0xE — stačí pro HDMI switch, ne pro EZPlay)
    - příjem: event ``esphome.cec_rx`` (opět jen při zapnutém bus logu)
    """

    def __init__(self, hass, service_base: str, gw_addr: int = 14) -> None:
        super().__init__()
        self._hass = hass
        self._base = service_base
        self._gw_addr = gw_addr
        self._remove_listener = None

    def _has(self, suffix: str) -> bool:
        return f"{self._base}{suffix}" in self._hass.services.async_services().get(
            "esphome", {}
        )

    async def async_start(self, callback: FrameCallback) -> None:
        await super().async_start(callback)
        self._remove_listener = self._hass.bus.async_listen(
            "esphome.cec_rx", self._on_event
        )

    async def async_stop(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        await super().async_stop()

    def _on_event(self, event) -> None:
        data = event.data
        raw = str(data.get("raw", ""))
        frame = frame_from_text(raw)
        if frame is not None:
            # source/destination z eventu jsou autoritativní
            self._deliver(
                int(data.get("source", frame[0])),
                int(data.get("destination", frame[1])),
                frame[2],
            )

    async def async_send(self, src: int, dst: int, payload: list[int]) -> None:
        if self._has("_cec_send_src"):
            await self._hass.services.async_call(
                "esphome",
                f"{self._base}_cec_send_src",
                {"source": src, "destination": dst, "data": list(payload)},
                blocking=True,
            )
            return
        if self._has("_cec_send"):
            if src != self._gw_addr:
                raise TransportError(
                    "gateway nemá akci cec_send_src (starý firmware); "
                    "EZPlay vyžaduje odesílatele TV — reflaschněte gateway "
                    "nebo použijte MQTT transport"
                )
            await self._hass.services.async_call(
                "esphome",
                f"{self._base}_cec_send",
                {"destination": dst, "data": list(payload)},
                blocking=True,
            )
            return
        raise TransportError(
            f"služba esphome.{self._base}_cec_send neexistuje "
            "(není nastavená ESPHome integrace?)"
        )


# ---------------------------------------------------------------------------
# HA hdmi_cec integrace (uživatel konfiguruje local libcec / remote pyCEC)
# ---------------------------------------------------------------------------
class HdmiCecTransport(CecTransport):
    """Používá HA integraci ``hdmi_cec`` tak, jak je nastavená uživatelem.

    - odeslání: služba ``hdmi_cec.send_command`` s ``raw`` (hlavička včetně
      zdrojové adresy → podvrh TV funguje)
    - příjem: komponenta hdmi_cec rx eventy nevysílá (konstanta
      ``cec_command_received`` je mrtvá); posloucháme ji defensivně, ale
      ``supports_rx = False`` → vendor stavy jsou optimistic, power se čte
      z hdmi_cec entity (typicky switch.hdmi_5)
    """

    supports_rx = False

    def __init__(self, hass) -> None:
        super().__init__()
        self._hass = hass
        self._remove_listener = None

    async def async_start(self, callback: FrameCallback) -> None:
        await super().async_start(callback)
        self._remove_listener = self._hass.bus.async_listen(
            "cec_command_received", self._on_event
        )

    async def async_stop(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        await super().async_stop()

    def _on_event(self, event) -> None:
        """Defensivní: hdmi_cec dnes event nevysílá, ale když začne, funguje rx."""
        data = event.data
        frame = None
        raw = data.get("raw")
        if raw:
            frame = frame_from_text(str(raw))
        else:
            try:
                att = data.get("att") or []
                frame = (
                    int(data.get("src", 0)),
                    int(data.get("dst", 0)),
                    [int(data.get("cmd", 0))]
                    + [int(a) & 0xFF for a in att],
                )
            except (TypeError, ValueError):
                frame = None
        if frame is not None:
            self._deliver(*frame)

    async def async_send(self, src: int, dst: int, payload: list[int]) -> None:
        if not self._hass.services.has_service("hdmi_cec", "send_command"):
            raise TransportError(
                "integrace hdmi_cec není nastavená — přidejte 'hdmi_cec:' "
                "do configuration.yaml (viz https://www.home-assistant.io/"
                "integrations/hdmi_cec/)"
            )
        # raw včetně hlavičky: CecCommand(raw) parsovaný komponentou
        await self._hass.services.async_call(
            "hdmi_cec",
            "send_command",
            {"raw": encode_pycec_message(src, dst, payload).strip()},
            blocking=True,
        )


def build_transport(hass, data: dict) -> CecTransport:
    """Factory podle config entry dat."""
    from .const import (  # noqa: PLC0415
        CONF_PREFIX,
        CONF_SERVICE_BASE,
        CONF_TRANSPORT,
        TRANSPORT_ESPHOME,
        TRANSPORT_ESPHOME_MQTT,
        TRANSPORT_HDMI_CEC,
    )

    transport = data[CONF_TRANSPORT]
    if transport == TRANSPORT_ESPHOME_MQTT:
        return MqttCecTransport(hass, data.get(CONF_PREFIX, "cec_gateway"))
    if transport == TRANSPORT_ESPHOME:
        return EspHomeCecTransport(hass, data[CONF_SERVICE_BASE])
    if transport == TRANSPORT_HDMI_CEC:
        return HdmiCecTransport(hass)
    raise ValueError(f"neznámý transport {transport}")
