"""CEC transporty: ESPHome gateway (MQTT / native API), pyCEC TCP, libcec local.

Všechny transporty jsou zdrojově transparentní — EZPlay vyžaduje odesílatele
TV (adresa 0) a přepnutí na HDMI IN naopak gateway (0xE). MQTT i pyCEC TCP
to umí vždy; ESPHome native až po reflashi s akcí ``cec_send_src``; libcec
posílá raw hlavičku vždy.
"""

from __future__ import annotations

import asyncio
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
# pyCEC TCP bridge (libcec remote) — protokol python3 -m pycec, port 9526
# ---------------------------------------------------------------------------
class PyCecTcpTransport(CecTransport):
    """Klient TCP bridgee pyCEC (github.com/konikvranik/pycec).

    Drátový protokol: řádky ``XX:XX:...`` (colon-hex včetně hlavičky)
    ukončené \\r\\n, oběma směry. Zdrojová adresa je podvržitelná v hlavičce.
    """

    RECONNECT_DELAY = 10.0

    def __init__(self, host: str, port: int = 9526) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def async_start(self, callback: FrameCallback) -> None:
        await super().async_start(callback)
        self._stopping = False
        await self._connect()
        self._task = asyncio.get_running_loop().create_task(self._read_loop())

    async def async_stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        await super().async_stop()

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )

    async def _read_loop(self) -> None:
        while not self._stopping:
            try:
                if self._reader is None:
                    await asyncio.sleep(self.RECONNECT_DELAY)
                    await self._connect()
                    continue
                line = await self._reader.readline()
                if not line:
                    raise ConnectionResetError("pyCEC bridge zavřel spojení")
                frame = frame_from_text(line.decode("utf-8", errors="replace"))
                if frame is not None:
                    self._deliver(*frame)
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionResetError) as exc:
                _LOGGER.warning("pyCEC bridge %s:%s odpojen (%s), obnovuji za %ss",
                                self._host, self._port, exc, self.RECONNECT_DELAY)
                self._reader = self._writer = None
                try:
                    await asyncio.sleep(self.RECONNECT_DELAY)
                    await self._connect()
                    _LOGGER.info("pyCEC bridge znovu připojen")
                except OSError:
                    pass  # další pokus ve smyčce

    async def async_send(self, src: int, dst: int, payload: list[int]) -> None:
        if self._writer is None:
            raise TransportError(
                f"pyCEC bridge {self._host}:{self._port} není připojen"
            )
        self._writer.write(encode_pycec_message(src, dst, payload).encode())
        await self._writer.drain()


# ---------------------------------------------------------------------------
# libcec local (systémový `cec` modul — swig binding z libcec)
# ---------------------------------------------------------------------------
class LibCecTransport(CecTransport):
    """Lokální libcec adapter (GPIO/USB CEC na stroji s HA).

    Vyžaduje systémový python modul ``cec`` (instaluje libcec; NIKOLI pip
    ``cec``). Volání shodná s CecAdapter z pycec: ICECAdapter.Create,
    CommandFromString, SetCommandCallback.
    """

    def __init__(self, adapter: str = "") -> None:
        super().__init__()
        self._adapter_name = adapter.strip()
        self._adapter = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def async_start(self, callback: FrameCallback) -> None:
        await super().async_start(callback)
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._init_libcec)

    def _init_libcec(self) -> None:
        try:
            import cec  # noqa: PLC0415 - systémový swig binding
        except ImportError as exc:
            raise TransportError(
                "systémový modul 'cec' (libcec) není k dispozici — viz README"
            ) from exc

        config = cec.libcec_configuration()
        config.strDeviceName = "ha-ax5125h"[:13]
        config.bActivateSource = 0
        # device type playback (3) — nechceme TV (0, drží ji opravdová TV)
        # ani audio system (5, drží ho soundbar)
        config.deviceTypes.Add(3)
        config.SetCommandCallback(self._libcec_command)

        adapter = cec.ICECAdapter.Create(config)
        found = list(adapter.DetectAdapters())
        com = None
        if self._adapter_name:
            for item in found:
                if item.strComName == self._adapter_name:
                    com = item.strComName
                    break
        elif found:
            com = found[0].strComName
        if com is None:
            raise TransportError(
                "libcec nenašel CEC adapter"
                + (f" ({self._adapter_name})" if self._adapter_name else "")
            )
        if not adapter.Open(com):
            raise TransportError(f"libcec se nepřipojil k adapteru {com}")
        self._adapter = adapter
        _LOGGER.info("libcec připojen k %s", com)

    def _libcec_command(self, command) -> None:
        """Callback z libcec vlákna — přeházet na event loop."""
        try:
            frame = frame_from_text(str(command))
        except Exception:  # noqa: BLE001 - binding může předat cokoli
            return
        if frame is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._deliver, *frame)

    async def async_send(self, src: int, dst: int, payload: list[int]) -> None:
        if self._adapter is None:
            raise TransportError("libcec adapter není inicializovaný")
        await asyncio.to_thread(self._transmit, src, dst, payload)

    def _transmit(self, src: int, dst: int, payload: list[int]) -> None:
        raw = encode_pycec_message(src, dst, payload).strip()
        cmd = self._adapter.CommandFromString(raw)
        if not self._adapter.Transmit(cmd):
            raise TransportError(f"libcec transmit selhal: {raw}")

    async def async_stop(self) -> None:
        if self._adapter is not None:
            await asyncio.to_thread(self._adapter.Close)
            self._adapter = None
        await super().async_stop()


def build_transport(hass, data: dict) -> CecTransport:
    """Factory podle config entry dat."""
    from .const import (  # noqa: PLC0415
        CONF_ADAPTER,
        CONF_HOST,
        CONF_PORT,
        CONF_PREFIX,
        CONF_SERVICE_BASE,
        CONF_TRANSPORT,
        TRANSPORT_ESPHOME,
        TRANSPORT_ESPHOME_MQTT,
        TRANSPORT_LIBCEC,
        TRANSPORT_PYCEC_TCP,
    )

    transport = data[CONF_TRANSPORT]
    if transport == TRANSPORT_ESPHOME_MQTT:
        return MqttCecTransport(hass, data.get(CONF_PREFIX, "cec_gateway"))
    if transport == TRANSPORT_ESPHOME:
        return EspHomeCecTransport(hass, data[CONF_SERVICE_BASE])
    if transport == TRANSPORT_PYCEC_TCP:
        return PyCecTcpTransport(data[CONF_HOST], int(data.get(CONF_PORT, 9526)))
    if transport == TRANSPORT_LIBCEC:
        return LibCecTransport(data.get(CONF_ADAPTER, ""))
    raise ValueError(f"neznámý transport {transport}")
