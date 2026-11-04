"""EzplayClient — session management, request/response matching, A5 push.

Bez závislostí na homeassistant (testovatelné). Transport vstřiknutý
(MQTT gateway / ESPHome native / pyCEC TCP / libcec local).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from . import ezplay as ez
from .transports import CecTransport, TransportError

_LOGGER = logging.getLogger(__name__)

#: Vendor session (spoof TV vendor ID) vydrží ~60 s — obnovujeme po 40 s.
SPOOF_TTL = 40.0


class EzplayError(Exception):
    """Základní chyba protokolu."""


class EzplayTimeout(EzplayError):
    """Soundbar neodpověděl v časovém limitu."""


class _Pending:
    """Čekající request: predikát rozhoduje, který příchozí rámec ho uzavře."""

    __slots__ = ("predicate", "future")

    def __init__(self, predicate: Callable[[list[int]], bool],
                 future: asyncio.Future):
        self.predicate = predicate
        self.future = future


class EzplayClient:
    """Stav soundbaru + EZPlay příkazy nad libovolným CEC transportem."""

    def __init__(
        self,
        transport: CecTransport,
        *,
        tv_addr: int = 0,
        sb_addr: int = 5,
        gw_addr: int = 14,
        hdmi_pa: tuple[int, int] = (0x31, 0x00),
        on_update: Callable[[dict], None] | None = None,
        session_delay: float = 0.5,
    ) -> None:
        self._transport = transport
        self._tv_addr = tv_addr
        self._sb_addr = sb_addr
        self._gw_addr = gw_addr
        self._hdmi_pa = hdmi_pa
        self._on_update = on_update
        self._session_delay = session_delay
        self._pending: list[_Pending] = []
        self._last_spoof: float | None = None
        self._support_queried = False
        #: Timeouty requestů (s) — přepsatelné pro testy.
        self.t_short = 1.5    # 0x8F / 0x71
        self.t_params = 2.5   # A0 -> A1
        self.t_item = 2.0     # A2 -> A4
        self.t_support = 2.0  # 0x90 -> 0x91
        #: Poslední známý stav (aktualizuje se i push zprávami A5/0x82).
        self.data: dict = {}

    # -- lifecycle -----------------------------------------------------------
    def set_update_callback(self, callback: Callable[[dict], None]) -> None:
        """Callback zavolaný při push změnách (A5, Active Source, ...)."""
        self._on_update = callback

    async def async_start(self) -> None:
        await self._transport.async_start(self._handle_frame)

    async def async_stop(self) -> None:
        for p in self._pending:
            if not p.future.done():
                p.future.cancel()
        self._pending.clear()
        await self._transport.async_stop()

    # -- low level -----------------------------------------------------------
    async def _send(self, src: int, dst: int, payload: list[int]) -> None:
        await self._transport.async_send(src, dst, payload)

    async def _request(
        self,
        src: int,
        dst: int,
        payload: list[int],
        predicate: Callable[[list[int]], bool],
        timeout: float,
    ) -> list[int] | None:
        """Odešle rámec a počká na odpověď splňující predikát."""
        loop = asyncio.get_running_loop()
        pending = _Pending(predicate, loop.create_future())
        self._pending.append(pending)
        try:
            await self._send(src, dst, payload)
            return await asyncio.wait_for(pending.future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            try:
                self._pending.remove(pending)
            except ValueError:
                pass

    async def _ensure_session(self) -> None:
        """Spoof TV vendor ID (otevírá EZPlay session) pokud je starý/vypršený."""
        now = time.monotonic()
        if self._last_spoof is None or now - self._last_spoof > SPOOF_TTL:
            await self._send(self._tv_addr, 0xF, ez.vendor_spoof_frame())
            self._last_spoof = now
            await asyncio.sleep(self._session_delay)

    # -- frame dispatch ------------------------------------------------------
    def _handle_frame(self, src: int, dst: int, payload: list[int]) -> None:
        """Všechny příchozí CEC rámce (voláno na event loop)."""
        if not payload:
            return
        opcode = payload[0]

        if opcode == ez.OP_EZPLAY and ez.is_ezplay(payload):
            self._handle_ezplay(src, payload)
        elif opcode == ez.OP_REPORT_POWER_STATUS and src == self._sb_addr:
            status = ez.parse_power_status(payload)
            if status:
                self.data["power"] = status
        elif opcode == ez.OP_REPORT_AUDIO_STATUS and src == self._sb_addr:
            audio = ez.parse_audio_status(payload)
            if audio:
                self.data["mute"], self.data["volume"] = audio
        elif opcode == ez.OP_ACTIVE_SOURCE and dst == 0xF:
            pa = ez.parse_physical_address(payload)
            if pa:
                self.data["active_source"] = ez.pa_to_str(pa)
                if pa == self._hdmi_pa:
                    self.data["input"] = "hdmi"
                elif pa == (0x00, 0x00):
                    self.data["input"] = "arc"

        self._resolve_pending(payload)
        if opcode in (ez.OP_EZPLAY, ez.OP_REPORT_POWER_STATUS,
                      ez.OP_REPORT_AUDIO_STATUS, ez.OP_ACTIVE_SOURCE):
            self._notify()

    def _handle_ezplay(self, src: int, payload: list[int]) -> None:
        sub = ez.ezplay_sub(payload)
        if src != self._sb_addr:
            return
        if sub == ez.SUB_ALL_PARAMS:
            params = ez.parse_all_params(payload)
            if params:
                self._apply_params(params)
        elif sub in (ez.SUB_ITEM_REPORT, ez.SUB_ITEM_CHANGED):
            item = ez.parse_item_value(payload)
            if item:
                # A4 nese raw hodnotu (dekódování s base 0x80 ji zvládne —
                # raw -5..+5 padne mimo offset okno), A5 inform má +0x7B
                base = 0x7B if sub == ez.SUB_ITEM_CHANGED else 0x80
                self._apply_item(item[0], item[1], base)
        elif sub == ez.SUB_SUPPORT_FEATURES:
            support = ez.parse_support_features(payload)
            if support:
                self.data["support_mask"], self.data["support_items"] = support

    def _apply_params(self, params: dict) -> None:
        self.data["eq"] = params["eq"]
        self.data["surr"] = params["surr"]
        self.data["bass"] = params["bass"]
        self.data["treble"] = params["treble"]
        self.data["dimmer"] = params["dimmer"]

    def _apply_item(self, item: int, value: int, base: int = 0x80) -> None:
        if item == ez.ITEM_EQ and 0 <= value < len(ez.EQ_OPTIONS):
            self.data["eq"] = value
        elif item == ez.ITEM_SURROUND and 0 <= value < len(ez.SURROUND_OPTIONS):
            self.data["surr"] = value
        elif item == ez.ITEM_DIMMER:
            self.data["dimmer"] = value
        elif item in (ez.ITEM_BASS, ez.ITEM_TREBLE):
            decoded = ez.decode_signed(value, base=base)
            key = "bass" if item == ez.ITEM_BASS else "treble"
            self.data[key] = max(ez.BASS_TREBLE_MIN,
                                 min(ez.BASS_TREBLE_MAX, decoded))

    def _resolve_pending(self, payload: list[int]) -> None:
        for pending in self._pending:
            if not pending.future.done() and pending.predicate(payload):
                pending.future.set_result(payload)
                return  # jeden rámec uzavře max. jeden request

    def _notify(self) -> None:
        if self._on_update:
            try:
                self._on_update(dict(self.data))
            except Exception:  # noqa: BLE001 - callback nesmí shodit dispatch
                _LOGGER.exception("on_update callback selhal")

    # -- veřejné API ---------------------------------------------------------
    async def async_refresh(self) -> dict:
        """Poll: power status -> (pokud zapnuto) all-params + audio status."""
        power = await self._request(
            self._tv_addr, self._sb_addr, ez.give_power_status_frame(),
            lambda p: (p[0] == ez.OP_REPORT_POWER_STATUS), self.t_short,
        )
        if power is not None:
            self.data["power"] = ez.parse_power_status(power) or "unknown"
        if self.data.get("power") in ("standby", "turning_off"):
            self.data["last_seen"] = time.time()
            return self.data

        params = None
        for _ in range(2):
            await self._ensure_session()
            resp = await self._request(
                self._tv_addr, self._sb_addr, ez.give_all_params_frame(),
                lambda p: ez.ezplay_sub(p) == ez.SUB_ALL_PARAMS, self.t_params,
            )
            if resp is not None:
                params = ez.parse_all_params(resp)
                if params:
                    break
            self._last_spoof = None  # vynutit čerstvý spoof pro retry
        if params is None:
            raise EzplayTimeout(
                "soundbar neodpovídá (A0 -> A1 timeout; session/CEC výpadek?)"
            )
        self._apply_params(params)

        if not self._support_queried:
            self._support_queried = True
            await self.async_get_support_features()

        audio = await self._request(
            self._tv_addr, self._sb_addr, ez.give_audio_status_frame(),
            lambda p: p[0] == ez.OP_REPORT_AUDIO_STATUS, self.t_short,
        )
        if audio is not None:
            parsed = ez.parse_audio_status(audio)
            if parsed:
                self.data["mute"], self.data["volume"] = parsed
        self.data["last_seen"] = time.time()
        return self.data

    async def async_get_support_features(self) -> tuple[int, int] | None:
        await self._ensure_session()
        resp = await self._request(
            self._tv_addr, self._sb_addr, ez.give_support_features_frame(),
            lambda p: ez.ezplay_sub(p) == ez.SUB_SUPPORT_FEATURES,
            self.t_support,
        )
        if resp is None:
            return None
        support = ez.parse_support_features(resp)
        if support:
            self.data["support_mask"], self.data["support_items"] = support
        return support

    async def async_set_item(self, item: int, value: int) -> int:
        """A2 zápis s čekáním na A4 ack; retry s čerstvým spoofem."""
        for attempt in range(2):
            await self._ensure_session()
            resp = await self._request(
                self._tv_addr, self._sb_addr, ez.set_item_frame(item, value),
                lambda p: (ez.ezplay_sub(p) == ez.SUB_ITEM_REPORT
                           and len(p) >= 7 and p[5] == item), self.t_item,
            )
            if resp is not None:
                ack = resp[6]
                self._apply_item(item, ack, base=0x80)
                return ack
            self._last_spoof = None
            _LOGGER.warning("A2 item %d timeout (pokus %d), obnovuji session",
                            item, attempt + 1)
        raise EzplayTimeout(f"zápis itemu {item} selhal (bez A4 ack)")

    async def async_set_input(self, which: str) -> None:
        """Přepnutí vstupu: 'arc' (SAM Request jako TV) / 'hdmi' (Active
        Source broadcast za gateway — soundbar přijímá routing jen od
        switch-role zařízení z eARC strany)."""
        which = which.lower()
        if which == "arc":
            await self._send(self._tv_addr, self._sb_addr,
                             ez.sam_request_frame())
        elif which == "hdmi":
            await self._send(self._gw_addr, 0xF, ez.active_source_frame(self._hdmi_pa))
        else:
            raise ValueError(f"neznámý vstup: {which}")
        self.data["input"] = which

    async def async_volume_key(self, code: int) -> None:
        """UCP stisk+puštění (volume up/down/mute)."""
        await self._send(self._tv_addr, self._sb_addr,
                         ez.user_control_pressed(code))
        await asyncio.sleep(0.1)
        await self._send(self._tv_addr, self._sb_addr,
                         ez.user_control_released(code))

    async def async_send_raw(self, src: int, dst: int, payload: list[int]) -> None:
        """Odešle libovolný rámec (pojistka na blokované subopcody výše)."""
        blocked = ez.blocked_ezplay_sub(payload)
        if blocked is not None:
            raise EzplayError(
                f"subopcode 0x{blocked:02X} je na blocklistu "
                "(destruktivní na AX5125H) — odmítám odeslat"
            )
        if (ez.ezplay_sub(payload) == ez.SUB_SET_ITEM_VALUE
                and len(payload) == 6):
            raise EzplayError(
                "A2 bez hodnoty není GET — zapíše garbage (pojistka); "
                "pro čtení použijte A3"
            )
        await self._send(src, dst, payload)
