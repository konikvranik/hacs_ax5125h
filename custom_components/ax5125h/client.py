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

#: Roku RTVR (Roku TV Ready) — vendor ID + subopcody (docs protocol-rtvr.md)
ROKU_OUI = (0x8A, 0xC7, 0x2E)
RTVR_ANNOUNCE_PRESENCE = 0x10
RTVR_INIT_SESSION = 0x11
RTVR_ACCEPT_SESSION = 0x12
RTVR_SET_STATE = 0x30
RTVR_REPORT_STATE = 0x31
RTVR_FSID = 0x5D
#: RTVR opt1 = SURROUND tri-state: 0=PURE, 1=PRO, 2=OFF (EZPlay item2 je
#: jen binární flag — jediná cesta k módu je RTVR poll/zápis)
RTVR_SURR_PURE = 0
RTVR_SURR_PRO = 1
RTVR_SURR_OFF = 2
#: Po neúspěšném RTVR handshake opakovat nejdřív za (s):
RTVR_RETRY_MIN = 300.0

#: Měkký standby po async_power_off trvá pár minut (tmavý displej, LED
#: svítí, CEC hlásí unmute) — během tohoto okna poll nesmí překlopit stav
#: zpět na on; skončí dřív, jakmile se objeví mute=1 (deep) nebo spontánní
#: push (bar zase žije).
SOFT_STANDBY_WINDOW = 360.0

#: Opkody, které jsou odpověďmi na CIZÍ poll (Give Power Status/Physical
#: Address/Vendor ID/OSD Name) — aktivitou uživatele NEJSOU. Soundbar na
#: ně odpovídá rámově (gateway/TV pinguje pravidelně), a kdyby obnovovaly
#: last_activity, rychlý vendor poll by se nikdy neuvolnil a bar by se
#: nedostal do hlubokého standby (bug 0.6.1).
_ACTIVITY_NOISE = frozenset({
    ez.OP_REPORT_POWER_STATUS,   # 0x90 — odpovědi na 0x8F heartbeat poll
    0x84,                        # Report Physical Address (odpověď na 0x83)
    ez.OP_DEVICE_VENDOR_ID,      # 0x87 — odpověď na 0x8C
    0x47,                        # Set OSD Name (odpověď na 0x46)
})


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
        #: RTVR (Roku) session — jen surround tri-state (PURE/PRO/OFF)
        self._rtvr_ok = False
        #: -inf = „ještě nebyl pokus“ — throttle nesmí záviset na tom, jak
        #: malý je monotonic čas (čerstvě nabootovaný host by první pokus
        #: s 0.0 potlačil na celých RTVR_RETRY_MIN)
        self._rtvr_last_attempt = float("-inf")
        #: Transport nedodává rx (hdmi_cec integrace)? Pak vendor zápisy jen
        #: optimistické a refresh neposílá dotazy (žádné timeouty).
        self.rx_capable = bool(getattr(transport, "supports_rx", True))
        #: Timeouty requestů (s) — přepsatelné pro testy.
        self.t_short = 1.5    # 0x8F / 0x71
        self.t_params = 2.5   # A0 -> A1
        self.t_item = 2.0     # A2 -> A4
        self.t_support = 2.0  # 0x90 -> 0x91
        self.t_rtvr = 2.0     # RTVR SetState -> ReportState
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
        elif opcode == ez.OP_REPORT_AUDIO_STATUS and (src == self._sb_addr or src == 5):
            audio = ez.parse_audio_status(payload)
            if audio is not None:
                self._apply_audio(
                    audio,
                    is_reply=any(
                        (not p.future.done()) and p.predicate(payload)
                        for p in self._pending
                    ),
                )
        elif opcode == ez.OP_ACTIVE_SOURCE and dst in (0xF, self._tv_addr):
            pa = ez.parse_physical_address(payload)
            if pa:
                self._apply_source_pa(pa)
        elif opcode == ez.OP_ROUTING_CHANGE and dst == 0xF:
            pa = ez.parse_routing_change(payload)
            if pa:
                self._apply_source_pa(pa)
        elif opcode == ez.OP_SET_STREAM_PATH and dst == 0xF:
            pa = ez.parse_physical_address(payload, opcode=ez.OP_SET_STREAM_PATH)
            if pa:
                self._apply_source_pa(pa)
        elif opcode == ez.OP_SET_SYSTEM_AUDIO_MODE and \
                src == self._sb_addr and dst == 0xF and len(payload) >= 2:
            # SAM On (5F:72:01) = soundbar se stal TV audio (ARC/eARC);
            # SAM Off (72:00) je přechodný — stav vstupu nemanipulujeme
            if payload[1] == 1:
                self.data["input"] = "arc"
        elif opcode == ez.OP_USER_CONTROL_PRESSED and dst == self._sb_addr and len(payload) >= 2:
            code = payload[1]
            if code == ez.UCP_VOLUME_UP and "volume" in self.data and self.data["volume"] is not None:
                self.data["volume"] = min(100, int(self.data["volume"]) + 1)
            elif code == ez.UCP_VOLUME_DOWN and "volume" in self.data and self.data["volume"] is not None:
                self.data["volume"] = max(0, int(self.data["volume"]) - 1)
            elif code == ez.UCP_MUTE_TOGGLE and "mute" in self.data and self.data["mute"] is not None:
                self.data["mute"] = not self.data["mute"]
            if self.rx_capable:
                asyncio.create_task(self._send_give_audio_status())

        if not self._resolve_pending(payload) and src == self._sb_addr \
                and opcode not in _ACTIVITY_NOISE:
            # spontánní rámec ze soundbaru = aktivita uživatele → rychlý
            # polling. ALE: odpovědi na cizí poll ne (gateway pinguje 0x8F
            # každých 5 s → flood 90:00 by aktivní mód nikdy neuvolnil a
            # rychlý vendor poll by baru zabránil zhluboka spát — bug 0.6.1)
            self.data["last_activity"] = time.time()
        if opcode in (ez.OP_EZPLAY, ez.OP_REPORT_POWER_STATUS,
                      ez.OP_REPORT_AUDIO_STATUS, ez.OP_ACTIVE_SOURCE,
                      ez.OP_ROUTING_CHANGE, ez.OP_SET_STREAM_PATH,
                      ez.OP_SET_SYSTEM_AUDIO_MODE,
                      ez.OP_USER_CONTROL_PRESSED):
            self._notify()

    def _apply_audio(self, audio: tuple[bool, int], is_reply: bool) -> None:
        """Aplikuje mute/volume z 0x7A a odvodí power (mute-bit model).

        Mute bit je jediný CEC stavový signál AX5125H, ale sam o sobě
        nerozliší „deep standby" od „on + ztlumený IR/HA mute". Rozlišujeme
        kontextem (0.5.1):
          - unmute → on; výjimka: okno SOFT_STANDBY_WINDOW po async_power_off
            (měkký standby hlásí unmute) — konec dřív při spontánním pushi
          - spontánní push (není odpověď na náš poll) = IR/HA akce → bar
            žije → vždy on (ztlumený push nemá vypínat stav!)
          - mute=1 z pollu platí standby jen při 0→1 přechodu (přechod do
            hlubokého standby neprodukuje žádný rámec); trvalý mute po
            pushi zůstává on
        """
        prev_mute = self.data.get("mute")
        self.data["mute"], self.data["volume"] = audio
        if not is_reply:
            # spontánní push: bar reaguje na IR/HA → živý = on; zároveň
            # ukončí případné potlačení po power_off a resetuje debounce
            self.data["power_suppress_until"] = 0.0
            self.data["last_activity"] = time.time()
            self.data["unmute_streak"] = 0
            self.data["power"] = "on"
        elif not audio[0]:
            # unmute z pollu: DEBOUNCE — flip standby→on až po 2 souvislých
            # vzorcích. Jednofázový flip tu vytvářel vlastní smyčku: poll
            # flip standby→on pustil rychlý vendor poll → ten držel bar
            # v měkkém standby (unmute) → nekonečné on/off oscilace.
            streak = (self.data.get("unmute_streak") or 0) + 1
            self.data["unmute_streak"] = streak
            if time.time() <= self.data.get("power_suppress_until", 0.0):
                self.data["unmute_streak"] = 0
            elif self.data.get("power") is None:
                # první poll po startu (žádný předchozí stav) — rovnou on,
                # debounce hlídá jen překlopení KNÁMÉHO standby (flap ochrana)
                self.data["power"] = "on"
            elif streak >= 2 and self.data.get("power") != "on":
                self.data["power"] = "on"
        elif prev_mute is not True:
            # poll viděl mute bez push signálu (0→1 přechod, nebo první
            # poll po startu s neznámým předchozím stavem) = hluboký standby
            self.data["unmute_streak"] = 0
            self.data["power"] = "standby"
            self.data["power_suppress_until"] = 0.0
        # mute=1 a už dřív mute (trvalý mute po pushi) → power necháme

    async def _send_give_audio_status(self) -> None:
        try:
            await self._send(self._tv_addr, self._sb_addr, ez.give_audio_status_frame())
        except Exception:
            pass

    def _apply_source_pa(self, pa: tuple[int, int]) -> None:
        """Active Source / Routing Change / Set Stream Path -> stav vstupu.

        Kotvy dle živých testů (docs soundbar-ax5125h): příchod na HDMI IN
        nese PA HDMI IN portu (typicky 3.1.0.0), PA 0.0.0.0 = aktivní zdroj
        je TV samotná (audio jde přes ARC/eARC).
        """
        self.data["active_source"] = ez.pa_to_str(pa)
        if pa == self._hdmi_pa:
            self.data["input"] = "hdmi"
        elif pa == (0x00, 0x00):
            self.data["input"] = "arc"

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
                # A4 i A5 nese +0x80 kódované bass/treble (živě: sweep
                # 0x7B(−5)→0x81(+1)); starý A5 base 0x7B byl mylný
                self._apply_item(item[0], item[1])
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

    def _apply_item(self, item: int, value: int) -> None:
        if item == ez.ITEM_EQ and 0 <= value < len(ez.EQ_OPTIONS):
            self.data["eq"] = value
        elif item == ez.ITEM_SURROUND:
            # binární inform: off → tri-state OFF; on → mód neznámý (RTVR
            # poll doplní), po OFF předpokládáme poslední známý mód
            if value:
                if self.data.get("surr") == 2:
                    self.data["surr"] = 0
            else:
                self.data["surr"] = 2
        elif item == ez.ITEM_DIMMER:
            self.data["dimmer"] = min(value, 3)      # menu 0–3
        elif item in (ez.ITEM_BASS, ez.ITEM_TREBLE):
            decoded = ez.decode_signed(value, base=0x80)
            key = "bass" if item == ez.ITEM_BASS else "treble"
            self.data[key] = max(ez.BASS_TREBLE_MIN,
                                 min(ez.BASS_TREBLE_MAX, decoded))

    def _resolve_pending(self, payload: list[int]) -> bool:
        """Vrací True, když rámec uzavřel některý čekající request."""
        for pending in self._pending:
            if not pending.future.done() and pending.predicate(payload):
                pending.future.set_result(payload)
                return True  # jeden rámec uzavře max. jeden request
        return False

    def _notify(self) -> None:
        if self._on_update:
            try:
                self._on_update(dict(self.data))
            except Exception:  # noqa: BLE001 - callback nesmí shodit dispatch
                _LOGGER.exception("on_update callback selhal")

    # -- veřejné API ---------------------------------------------------------
    async def async_refresh(self, audio_only: bool = False) -> dict:
        """Poll: audio status (power/volume/mute) -> (pokud živý) all-params.

        audio_only=True = jen lehký 0x71 probe (používá coordinator v idle/
        standby fázi) — vendor provoz by držel bar v měkkém standby navždy,
        takže by se nikdy nedostal do hlubokého standby, jehož mute bit je
        jediný spolehlivý stavový signál.

        Power model (živě 2026-11-05, docs research-log #12): CEC 0x8F hlásí
        ON vždy (soft i deep standby) — nepoužitelné. Jediný CEC rozlišovač
        stavu AX5125H je mute bit v 0x71 Report Audio Status, vyhodnocovaný
        kontextově v _apply_audio (přechody + push detekce): spontánní push
        = on (bar živý, i ztlumený), poll mute přechod = deep standby,
        trvalý mute po pushi = zůstává on, unmute v soft-standby okně po
        power_off = zůstává standby.
        """
        if not self.rx_capable:
            # rx-less transport: žádné dotazy (odpověď by stejně nedošla);
            # power doplňuje coordinator z entity, vendor stavy optimistic
            self.data["last_seen"] = time.time()
            return self.data

        audio = await self._request(
            self._tv_addr, self._sb_addr, ez.give_audio_status_frame(),
            lambda p: p[0] == ez.OP_REPORT_AUDIO_STATUS, self.t_short,
        )
        parsed = ez.parse_audio_status(audio) if audio is not None else None
        if parsed is None:
            # 0x71 odpovídá v KAŽDÉM stavu (i deep standby) — ticho znamená
            # mrtvý CEC transport/session, ne standby
            raise EzplayTimeout(
                "soundbar neodpovídá na 0x71 (CEC výpadek?)"
            )
        # POZOR: mute/volume/power už aplikoval _handle_frame (jediné místo,
        # kde jde správně rozlišit reply vs. spontánní push) — tady znovu
        # NEAPLIKUJEME, dvojí aplikace rozbíjí debounce (0.6.1).
        muted = bool(self.data.get("mute"))

        if audio_only or (muted and self.data.get("power") != "on"):
            # deep standby (mute přechod bez push) / idle fáze — vendor
            # provoz šetříme (i kvůli hlubokému standby samotného baru)
            self.data["last_seen"] = time.time()
            return self.data

        params = None
        for _ in range(2):
            await self._ensure_session()
            resp = await self._request(
                self._tv_addr, self._sb_addr, ez.give_all_params_frame(),
                lambda p: ez.ezplay_sub(p) == ez.SUB_ALL_PARAMS,
                self.t_params,
            )
            if resp is not None:
                params = ez.parse_all_params(resp)
                if params:
                    break
            self._last_spoof = None  # vynutit čerstvý spoof pro retry
        if params is None:
            # vendor mlčí, ale 0x71 odpověděla unmute → bar žije (jen
            # vendor session wedge / ne-CEC vstup) — nefailujeme, stav
            # hlasitosti je platný, parametry jen neaktuální
            _LOGGER.debug(
                "A0 -> A1 timeout, ale audio status živý — vracím audio stav"
            )
            self.data["last_seen"] = time.time()
            return self.data
        self._apply_params(params)

        if not self._support_queried:
            self._support_queried = True
            await self.async_get_support_features()

        # RTVR poll (surround tri-state PURE/PRO/OFF) — nikdy nerozbíjí
        # hlavní refresh (při mrtvé session handshake max 1×/5 min)
        surr = await self.rtvr_poll()
        if surr is not None:
            self.data["surr"] = surr

        self.data["last_seen"] = time.time()
        return self.data

    async def _vendor_alive(self) -> bool:
        """Odpovídá EZPlay vrstva? Jediný pokus A0 -> A1 (bez retry cyklu).

        Původně se používal k rozlišení fake-standby na ne-CEC vstupech —
        od 0.5.0 se power řeší přes mute bit (refresh); metoda zůstává
        jako veřejný liveness helper pro testy/diagnostiku.
        """
        await self._ensure_session()
        resp = await self._request(
            self._tv_addr, self._sb_addr, ez.give_all_params_frame(),
            lambda p: ez.ezplay_sub(p) == ez.SUB_ALL_PARAMS, self.t_params,
        )
        params = ez.parse_all_params(resp) if resp is not None else None
        if params:
            self._apply_params(params)
        return bool(params)

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
        """A2 zápis s čekáním na A4 ack; retry s čerstvým spoofem.

        rx-less transport: ack nečekáme (nedojde), stav aplikujeme optimisticky.
        """
        self.data["last_activity"] = time.time()
        if not self.rx_capable:
            await self._ensure_session()
            await self._send(self._tv_addr, self._sb_addr,
                             ez.set_item_frame(item, value))
            self._apply_item(item, value & 0xFF)
            return value & 0xFF
        self.data["last_activity"] = time.time()
        for attempt in range(2):
            await self._ensure_session()
            resp = await self._request(
                self._tv_addr, self._sb_addr, ez.set_item_frame(item, value),
                lambda p: (ez.ezplay_sub(p) == ez.SUB_ITEM_REPORT
                           and len(p) >= 7 and p[5] == item), self.t_item,
            )
            if resp is not None:
                ack = resp[6]
                self._apply_item(item, ack)
                return ack
            self._last_spoof = None
            _LOGGER.warning("A2 item %d timeout (pokus %d), obnovuji session",
                            item, attempt + 1)
        raise EzplayTimeout(f"zápis itemu {item} selhal (bez A4 ack)")

    async def async_set_input(self, which: str) -> None:
        """Přepnutí vstupu: 'arc' (SAM Request jako TV) / 'hdmi' (Active
        Source broadcast za gateway — soundbar přijímá routing jen od
        switch-role zařízení z eARC strany)."""
        self.data["last_activity"] = time.time()
        which = which.lower()
        if which == "arc":
            await self._send(self._tv_addr, self._sb_addr,
                             ez.sam_request_frame())
        elif which == "hdmi":
            await self._send(self._gw_addr, 0xF, ez.active_source_frame(self._hdmi_pa))
        else:
            raise ValueError(f"neznámý vstup: {which}")
        self.data["input"] = which

    async def async_power_on(self) -> None:
        """Zapne soundbar: UCP Power On (0x6D) + verifikace + poctivý revert.

        Wake matice (živě, research-log #14): **UCP 0x6D press/release je
        dvakrát ověřený wake z hlubokého standby** (bar odpoví 72:01 SAM on
        + 7A unmute). Set Stream Path i Active Source z hlubokého standby
        NEZABÍRAJÍ (sweep W1-W5: ticho přes 5 minut stabilního mute=1);
        z měkké fáze neznám fungující rámec žádný. Po odeslání verifikujeme
        poll 0x71 (unmute = probuzeno); když mute přetrvává, zkusíme ještě
        SP+AS, a pokud ani pak nic, stav se poctivě vrací na standby.
        """
        self.data["last_activity"] = time.time()
        await self._send(
            self._tv_addr, self._sb_addr, ez.user_control_pressed(ez.UCP_POWER_ON)
        )
        await asyncio.sleep(0.15)
        await self._send(
            self._tv_addr, self._sb_addr, ez.user_control_released(ez.UCP_POWER_ON)
        )
        self.data["power"] = "on"
        self.data["power_suppress_until"] = 0.0
        self._notify()
        if not self.rx_capable:
            return
        woke = False
        for attempt in range(4):
            await asyncio.sleep(2.5)
            if attempt == 1:
                # druhá fáze: SP+AS (zkus i měkkou cestu; z deep neškodí —
                # sweep: ticho)
                await self._send(self._tv_addr, 0xF,
                                 ez.set_stream_path_frame(self._hdmi_pa))
                await self._send(self._gw_addr, 0xF,
                                 ez.active_source_frame(self._hdmi_pa))
            audio = await self._request(
                self._tv_addr, self._sb_addr, ez.give_audio_status_frame(),
                lambda p: p[0] == ez.OP_REPORT_AUDIO_STATUS, self.t_short,
            )
            parsed = ez.parse_audio_status(audio) if audio is not None else None
            if parsed is not None:
                # stav už aplikoval _handle_frame — tady jen kontrola unmute
                if not parsed[0]:
                    woke = True
                    break
        if not woke:
            _LOGGER.info(
                "power ON nepotvrzen (0x71 stále mute) — vracím stav standby"
            )
            self.data["power"] = "standby"
        self._notify()

    async def async_power_off(self) -> None:
        """Převede soundbar / receiver do standby (CEC 0x36 Standby).

        Měkký standby fáze (pár minut, CEC hlásí unmute) nesmí poll překlopit
        zpět na on — nastavíme potlačení SOFT_STANDBY_WINDOW; skončí dřív při
        mute=1 (deep) nebo spontánním pushi (bar zase žije).
        """
        self.data["last_activity"] = time.time()
        await self._send(self._tv_addr, self._sb_addr, ez.standby_frame())
        self.data["power"] = "standby"
        self.data["power_suppress_until"] = time.time() + SOFT_STANDBY_WINDOW
        self._notify()

    async def async_set_dimmer(self, menu: int) -> None:
        """Jas displeje: menu 0–3 (raw zápis; wire clampne na 0–2)."""
        await self.async_set_item(ez.ITEM_DIMMER, max(0, min(3, int(menu))))

    # -- RTVR (Roku TV Ready) — surround tri-state PURE/PRO/OFF --------------
    @staticmethod
    def _is_rtvr_frame(payload: list[int], sub: int) -> bool:
        return (len(payload) >= 6 and payload[0] == ez.OP_EZPLAY
                and tuple(payload[1:4]) == ROKU_OUI and payload[4] == sub)

    async def rtvr_handshake(self) -> bool:
        """RTVR handshake (docs protocol-rtvr.md): spoof Roku vendor,
        AnnouncePresence v3.0, InitSession, AcceptSession.

        Side-efekt: zařízení aplikuje uložené defaulty (pozorováno —
        viz limits-neural-virtual.md); běží 1× při startu session.
        """
        self._last_spoof = None   # další EZPlay dotaz udělá čerstvý spoof
        await self._send(self._tv_addr, 0xF,
                         [ez.OP_DEVICE_VENDOR_ID, *ROKU_OUI])
        await asyncio.sleep(0.3)
        announce = [ez.OP_EZPLAY, *ROKU_OUI, RTVR_ANNOUNCE_PRESENCE, 0x30]
        await self._send(self._tv_addr, 0xF, announce)
        init = await self._request(
            self._tv_addr, self._sb_addr, announce,
            lambda p: (self._is_rtvr_frame(p, RTVR_INIT_SESSION)
                       and len(p) >= 8),
            self.t_rtvr,
        )
        if init is None:
            _LOGGER.debug("RTVR InitSession nedorazila")
            return False
        # AcceptSession: verze 0x30 + 9 bajtů serial (obsah nekritický)
        await self._send(self._tv_addr, self._sb_addr,
                         [ez.OP_EZPLAY, *ROKU_OUI, RTVR_ACCEPT_SESSION,
                          0x30, 0x52, 0x4F, 0x4B, 0x55, 0x54, 0x56, 0x48,
                          0x41, 0x00])
        await asyncio.sleep(0.3)
        return True

    async def rtvr_poll(self) -> int | None:
        """RTVR poll → surround tri-state (0=PURE, 1=PRO, 2=OFF) | None.

        Mrtvá session → handshake (max 1× za RTVR_RETRY_MIN), pak znovu.
        """
        if not self.rx_capable:
            return None
        now = time.monotonic()
        if not self._rtvr_ok:
            if now - self._rtvr_last_attempt < RTVR_RETRY_MIN:
                return None
            self._rtvr_last_attempt = now
            if not await self.rtvr_handshake():
                return None
            self._rtvr_ok = True
        r = await self._request(
            self._tv_addr, self._sb_addr,
            [ez.OP_EZPLAY, *ROKU_OUI, RTVR_SET_STATE],
            lambda p: (self._is_rtvr_frame(p, RTVR_REPORT_STATE)
                       and len(p) >= 9),
            self.t_rtvr,
        )
        if r is None:
            self._rtvr_ok = False   # session zanikla → příště handshake
            return None
        opts = (r[6] >> 4, r[6] & 0xF, r[7] >> 4, r[7] & 0xF,
                r[8] >> 4, r[8] & 0xF)
        surr = opts[1]
        if surr in (0, 1, 2):
            self.data["surr"] = surr
        return surr if surr in (0, 1, 2) else None

    async def rtvr_set_surr(self, mode: int) -> bool:
        """Zápis surround módu přes RTVR SetState (0=PURE, 1=PRO, 2=OFF).

        Vrací True, když potvrzený ReportState nese zapsanou hodnotu.
        """
        if mode not in (0, 1, 2):
            raise ValueError(f"neznámý surround mód: {mode}")
        if not self.rx_capable:
            return False
        now = time.monotonic()
        if not self._rtvr_ok:
            if now - self._rtvr_last_attempt < RTVR_RETRY_MIN:
                return False
            self._rtvr_last_attempt = now
            if not await self.rtvr_handshake():
                return False
            self._rtvr_ok = True
        r = await self._request(
            self._tv_addr, self._sb_addr,
            [ez.OP_EZPLAY, *ROKU_OUI, RTVR_SET_STATE, RTVR_FSID,
             0xF0 | mode, 0xFF, 0xFF],
            lambda p: (self._is_rtvr_frame(p, RTVR_REPORT_STATE)
                       and len(p) >= 9),
            self.t_rtvr,
        )
        if r is None:
            self._rtvr_ok = False
            return False
        surr = r[6] & 0xF   # opt1 = low nible prvního option bajtu
        if surr in (0, 1, 2):
            self.data["surr"] = surr
            self._notify()
        return surr == mode

    async def async_source_pressed(self) -> str:
        """IR SOURCE stisk — posune vstupní cyklus (optimistický tracking).

        OPT/AUX/USB/BT nevysílají na CEC nic, přepnutí na ně se detekuje
        počítáním stisků SOURCE z IR automatizace (service source_pressed).
        Neznámý stav: default po bootu je ARC.
        """
        current = self.data.get("input")
        if current not in ez.INPUT_CYCLE:
            current = "arc"
        nxt = ez.INPUT_CYCLE[
            (ez.INPUT_CYCLE.index(current) + 1) % len(ez.INPUT_CYCLE)
        ]
        self.data["input"] = nxt
        self._notify()
        return nxt

    async def async_set_mute(self, enable: bool) -> None:
        """Nastaví stav mute (přepne UCP Mute pokud stav neodpovídá)."""
        self.data["last_activity"] = time.time()
        current = self.data.get("mute")
        if current is None or current != enable:
            await self.async_volume_key(ez.UCP_MUTE_TOGGLE)
        self.data["mute"] = enable
        self._notify()

    async def async_set_volume(self, value: int) -> None:
        """Absolutní hlasitost 0–100 (EZPlay Set_Volume; živě ověřeno 0.4.2).

        FW: worker 0x1013a0 — raw scale, obj[7], >100 clampne HW na 100.
        Bez acku; potvrzení je push 0x7A (handler už ho aplikuje) nebo
        explicitní 0x71 readback. Optimisticky aplikujeme hned.
        """
        self.data["last_activity"] = time.time()
        value = max(0, min(100, int(value)))
        await self._ensure_session()
        await self._send(self._tv_addr, self._sb_addr, ez.set_volume_frame(value))
        self.data["volume"] = value
        self._notify()
        if self.rx_capable:
            audio = await self._request(
                self._tv_addr, self._sb_addr, ez.give_audio_status_frame(),
                lambda p: p[0] == ez.OP_REPORT_AUDIO_STATUS, self.t_short,
            )
            if audio is not None:
                parsed = ez.parse_audio_status(audio)
                if parsed is not None:
                    self.data["mute"], self.data["volume"] = parsed
                    self._notify()

    async def async_volume_key(self, code: int) -> None:
        """UCP stisk+puštění (volume up/down/mute) s aktualizací stavu."""
        self.data["last_activity"] = time.time()
        await self._send(self._tv_addr, self._sb_addr,
                         ez.user_control_pressed(code))
        await asyncio.sleep(0.1)
        await self._send(self._tv_addr, self._sb_addr,
                         ez.user_control_released(code))
        if self.rx_capable:
            audio = await self._request(
                self._tv_addr, self._sb_addr, ez.give_audio_status_frame(),
                lambda p: p[0] == ez.OP_REPORT_AUDIO_STATUS, self.t_short,
            )
            if audio is not None:
                parsed = ez.parse_audio_status(audio)
                if parsed is not None:
                    self.data["mute"], self.data["volume"] = parsed
                    self._notify()
                    return
        # Optimistická aktualizace pokud dotaz neodpověděl
        if code == ez.UCP_VOLUME_UP and "volume" in self.data and self.data["volume"] is not None:
            self.data["volume"] = min(100, int(self.data["volume"]) + 1)
            self._notify()
        elif code == ez.UCP_VOLUME_DOWN and "volume" in self.data and self.data["volume"] is not None:
            self.data["volume"] = max(0, int(self.data["volume"]) - 1)
            self._notify()
        elif code == ez.UCP_MUTE_TOGGLE and "mute" in self.data and self.data["mute"] is not None:
            self.data["mute"] = not self.data["mute"]
            self._notify()

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
