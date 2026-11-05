"""EZPlay / Hisense vendor CEC protocol codec (pure functions, no HA imports).

Reverzně inženýrenská znalost protokolu soundbaru Hisense AX5125H (listopad
2025; firmware AX3120G statická analýza + živé testy). Viz README.

CEC rámec vendor vrstvy::

    [src:dst] A0 A8 82 00 <subopcode> [payload...]
              └ OUI Hisense ┘

- Odesílatel musí být TV (adresa 0) s vendor ID Hisense — před použitím je
  nutné broadcastnout ``87 A8 82 00`` (Device Vendor ID), session ~60 s.
- Rozsah subopcodů 0x90–0xB5; vše ostatní soundbar ignoruje.
"""

from __future__ import annotations

import re
from typing import Sequence

# --- CEC opcodes ------------------------------------------------------------
OP_DEVICE_VENDOR_ID = 0x87
OP_EZPLAY = 0xA0                       # Hisense "Vendor Command With ID"
OP_SYSTEM_AUDIO_MODE_REQUEST = 0x70
OP_SET_SYSTEM_AUDIO_MODE = 0x72        # SB broadcast 5F:72:01 = SAM/ARC on
OP_ROUTING_CHANGE = 0x80               # 5F:80 <old_pa:2> <new_pa:2>
OP_SET_STREAM_PATH = 0x86              # 0F:86 <pa:2>
OP_ACTIVE_SOURCE = 0x82
OP_GIVE_DEVICE_POWER_STATUS = 0x8F
OP_REPORT_POWER_STATUS = 0x90
OP_STANDBY = 0x36
OP_USER_CONTROL_PRESSED = 0x44
OP_USER_CONTROL_RELEASED = 0x45
OP_GIVE_AUDIO_STATUS = 0x71
# Odpověď na Give Audio Status je 0x7A (živě: `50:7A:<mute<<7|vol>`).
# 0x7D je System Audio Mode Status — stará hodnota 0x7D nikdy nedorazila,
# takže volume/mute zůstávaly navždy unknown.
OP_REPORT_AUDIO_STATUS = 0x7A

HISENSE_OUI = (0xA8, 0x82, 0x00)

# --- EZPlay subopcodes ------------------------------------------------------
SUB_GIVE_SUPPORT_FEATURES = 0x90       # -> 91 <mask> <count>
SUB_SUPPORT_FEATURES = 0x91
SUB_GIVE_DEVICEID = 0x94               # -> 95 <BT MAC> (neověřeno)
SUB_GIVE_ALL_PARAMS = 0xA0             # -> A1 (nejspolehlivější čtení)
SUB_ALL_PARAMS = 0xA1
SUB_SET_ITEM_VALUE = 0xA2              # <item> <val>  -> A4 <item> <val>
SUB_GET_ITEM_VALUE = 0xA3              # <item>        -> A4 <item> <val>
SUB_ITEM_REPORT = 0xA4
SUB_ITEM_CHANGED = 0xA5                # push při změně z menu/ovladače!
SUB_RESET_ALL_PARAMS = 0xA6            # DESTRUKTIVNI - nikdy neposílat
SUB_SET_VOLUME = 0xAC                  # neověřený formát hodnoty (holý -> vol=0)
SUB_GIVE_ALL_ITEM_RANGE = 0xB0         # DESTRUKTIVNI na AX5125H!
SUB_GIVE_ITEM_RANGE = 0xB2             # DESTRUKTIVNI na AX5125H!
SUB_SET_SWITCH_FEATURES = 0xB4         # feature 2 = Symphony
SUB_REPORT_SWITCH_FEATURES = 0xB6

#: Subopcody, které integrace odmítne odeslat (pojistka proti bricknutí).
BLOCKED_SUBOPCODES = frozenset(
    {SUB_RESET_ALL_PARAMS, SUB_GIVE_ALL_ITEM_RANGE, SUB_GIVE_ITEM_RANGE}
)

# --- items ------------------------------------------------------------------
# Mapování item id živě ověřeno A/B testem (2026-11-05): surround změna
# pohnula item 2, bas item 3, treble item 4, jas displeje item 5. Staré
# mapování (2=BASS, 4=DIMMER, 5=SURR) bylo mylné — a A2 zápisy na špatné
# itemy korbupovaly NVRAM (RTVR hodnoty 11–13 mimo rozsah 0–10).
ITEM_EQ = 1
ITEM_SURROUND = 2   # 0=off, 1=on — PURE vs PRO EZPlay nerozlišuje (jen RTVR)
ITEM_BASS = 3       # +0x80 kódování: 0x7D=−3, 0x80=0, 0x81=+1 (A1/A4/A5 i A2)
ITEM_TREBLE = 4     # +0x80 kódování
ITEM_DIMMER = 5     # menu 0–3 raw zápis; wire clampne na 0–2 (menu 3 čte 2)

#: EQ (item 1) — EZPlay hodnoty (0-based). POZOR: RTVR vrstva má jiné číslování!
EQ_OPTIONS = ("MUSIC", "MOVIE", "NEWS", "SPORT", "NIGHT", "GAME", "AI EQ")

#: SURROUND (item 2) — binární on/off na EZPlay wire
SURROUND_OPTIONS = ("OFF", "ON")

BASS_TREBLE_MIN = -5
BASS_TREBLE_MAX = 5

#: Cyklus vstupů při IR SOURCE stisku (ARC→HDMI IN→OPTICAL→AUX→USB→BT).
#: OPT/AUX/USB/BT na CEC nevysílají vůbec nic (docs soundbar-ax5125h,
#: definitivně prozkoumáno) — jejich stav jde držet jen počítáním stisků;
#: CEC kotvy (SAM On, Routing Change/Active Source) stav korigují zpět.
INPUT_CYCLE = ("arc", "hdmi", "opt", "aux", "usb", "bt")

#: UCP kódy hlasitosti a napájení
UCP_VOLUME_UP = 0x41
UCP_VOLUME_DOWN = 0x42
UCP_MUTE_TOGGLE = 0x43
UCP_POWER = 0x40
UCP_POWER_ON = 0x6D
UCP_POWER_OFF = 0x6C

POWER_STATUSES = {0: "on", 1: "standby", 2: "turning_on", 3: "turning_off"}


# --- tvary rámců (payload bez hlavičky) -------------------------------------
def vendor_spoof_frame() -> list[int]:
    """Broadcast Device Vendor ID — podvrh TV jako Hisense (otevře session)."""
    return [OP_DEVICE_VENDOR_ID, *HISENSE_OUI]


def ezplay_frame(sub: int, payload: Sequence[int] = ()) -> list[int]:
    """Sestaví EZPlay payload ``A0 A8 82 00 <sub> <payload...>``."""
    return [OP_EZPLAY, *HISENSE_OUI, sub, *payload]


def give_all_params_frame() -> list[int]:
    return ezplay_frame(SUB_GIVE_ALL_PARAMS)


def get_item_frame(item: int) -> list[int]:
    return ezplay_frame(SUB_GET_ITEM_VALUE, [item])


def set_item_frame(item: int, value: int) -> list[int]:
    return ezplay_frame(SUB_SET_ITEM_VALUE, [item, value & 0xFF])


def give_support_features_frame() -> list[int]:
    return ezplay_frame(SUB_GIVE_SUPPORT_FEATURES)


def sam_request_frame(pa: tuple[int, int] = (0x00, 0x00)) -> list[int]:
    """System Audio Mode Request — přepne soundbar na ARC (odesílatel TV)."""
    return [OP_SYSTEM_AUDIO_MODE_REQUEST, *pa]


def active_source_frame(pa: tuple[int, int]) -> list[int]:
    """Active Source broadcast — přepne na HDMI IN (odesílatel gateway/switch)."""
    return [OP_ACTIVE_SOURCE, *pa]


def give_power_status_frame() -> list[int]:
    return [OP_GIVE_DEVICE_POWER_STATUS]


def standby_frame() -> list[int]:
    return [OP_STANDBY]


def give_audio_status_frame() -> list[int]:
    return [OP_GIVE_AUDIO_STATUS]


def user_control_pressed(code: int) -> list[int]:
    return [OP_USER_CONTROL_PRESSED, code]


def user_control_released(code: int) -> list[int]:
    return [OP_USER_CONTROL_RELEASED, code]


# --- parsování --------------------------------------------------------------
def is_ezplay(payload: list[int]) -> bool:
    return (
        len(payload) >= 5
        and payload[0] == OP_EZPLAY
        and tuple(payload[1:4]) == HISENSE_OUI
    )


def ezplay_sub(payload: list[int]) -> int | None:
    """Subopcode EZPlay rámce (None pokud to není EZPlay)."""
    if not is_ezplay(payload) or len(payload) < 5:
        return None
    return payload[4]


def blocked_ezplay_sub(payload: list[int]) -> int | None:
    """Vrátí subopcode, pokud je rámec na blocklistu (pojistka)."""
    sub = ezplay_sub(payload)
    return sub if sub in BLOCKED_SUBOPCODES else None


def decode_signed(value: int, base: int = 0x80) -> int:
    """Dekóduje BASS/TREBLE hodnotu z reportů.

    Tři formáty podle zdroje (viz mappings):
    - A1/A3 po RTVR zápisu/boot: offset +0x80 (0x80 = 0)
    - A4 po A2 zápisu:           raw two's complement (00 = 0)
    - A5 inform (menu/ovladač):  offset +0x7B (0x7B = 0)

    Nejprve zkusí offset okno kolem ``base``; mimo okno bere raw signed byte.
    """
    lo, hi = base - 5, base + 5
    if lo <= value <= hi:
        return value - base
    return value - 256 if value > 127 else value


def encode_bass_treble(value: int) -> int:
    """Zápis BASS/TREBLE přes A2: +0x80 kódování (−3 → 0x7D, 0 → 0x80).

    Živě ověřeno (2026-11-05): IR změna bas −3 se na wire projevila jako
    0x7D; A2 SET interpretuje hodnotu jako +0x80 formu (obj = val − 0x7B,
    interní škála 0–10 = value+5). Two's complement hodnoty (0xFD apod.)
    zapisovaly nevalidní obj → NVRAM korupce (RTVR 11–13).
    """
    return (value + 0x80) & 0xFF


def parse_all_params(payload: list[int]) -> dict | None:
    """A1 [EQ][SURR][BASS+0x80][TRE+0x80][DIM 1-4] -> stavový dict."""
    if ezplay_sub(payload) != SUB_ALL_PARAMS or len(payload) < 10:
        return None
    return {
        "eq": payload[5],
        "surr": payload[6],
        "bass": decode_signed(payload[7], base=0x80),
        "treble": decode_signed(payload[8], base=0x80),
        "dimmer": payload[9],
    }


def parse_item_value(payload: list[int]) -> tuple[int, int] | None:
    """A4/A5 <item> <val> -> (item, val)."""
    if ezplay_sub(payload) not in (SUB_ITEM_REPORT, SUB_ITEM_CHANGED):
        return None
    if len(payload) < 7:
        return None
    return payload[5], payload[6]


def parse_support_features(payload: list[int]) -> tuple[int, int] | None:
    """91 <mask> <count> -> (maska skupin, počet itemů)."""
    if ezplay_sub(payload) != SUB_SUPPORT_FEATURES or len(payload) < 7:
        return None
    return payload[5], payload[6]


def parse_power_status(payload: list[int]) -> str | None:
    """0x90 <status> -> on/standby/turning_on/turning_off."""
    if not payload or payload[0] != OP_REPORT_POWER_STATUS or len(payload) < 2:
        return None
    return POWER_STATUSES.get(payload[1], "unknown")


def parse_audio_status(payload: list[int]) -> tuple[bool, int] | None:
    """0x7D <mute<<7|volume> -> (mute, volume 0-100)."""
    if not payload or payload[0] != OP_REPORT_AUDIO_STATUS or len(payload) < 2:
        return None
    return bool(payload[1] & 0x80), payload[1] & 0x7F


def parse_physical_address(
    payload: list[int], opcode: int = OP_ACTIVE_SOURCE
) -> tuple[int, int] | None:
    """0x82 / 0x86 <pa_hi> <pa_lo> -> 2bajtová fyzická adresa."""
    if not payload or payload[0] != opcode or len(payload) < 3:
        return None
    return payload[1], payload[2]


def parse_routing_change(payload: list[int]) -> tuple[int, int] | None:
    """0x80 <old_pa:2> <new_pa:2> -> nová (cílová) fyzická adresa."""
    if not payload or payload[0] != OP_ROUTING_CHANGE or len(payload) < 5:
        return None
    return payload[3], payload[4]


def pa_to_str(pa: tuple[int, int]) -> str:
    return f"{pa[0] >> 4}.{pa[0] & 0xF}.{pa[1] >> 4}.{pa[1] & 0xF}"


def pa_from_str(text: str) -> tuple[int, int]:
    """'3.1.0.0' -> (0x31, 0x00); ValueError při špatném formátu."""
    parts = text.strip().split(".")
    if len(parts) != 4 or not all(p in "0123456789ABCDEFabcdef" for p in parts):
        raise ValueError(f"neplatná fyzická adresa: {text!r}")
    nibbles = [int(p, 16) for p in parts]
    return (nibbles[0] << 4) | nibbles[1], (nibbles[2] << 4) | nibbles[3]


# --- textové formáty transportů ---------------------------------------------
_FRAME_START_RE = re.compile(r"^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2})*)")
_FRAME_MARKER_RE = re.compile(r"(?:>>|<<)\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2})+)")


def frame_from_text(text: str) -> tuple[int, int, list[int]] | None:
    """(src, dst, payload) z textové podoby rámce.

    Podporované vstupy:
    - gateway MQTT rx:  ``50:90:00 => AudioSystem to TV: <Report Power Status>``
    - pyCEC TCP bridge: ``50:90:00``
    - libcec log:       ``TRAFFIC: [ 12345] >> 50:90:00`` / ``<< 50:90:00``
    """
    text = text.strip()
    m = _FRAME_START_RE.match(text) or _FRAME_MARKER_RE.search(text)
    if not m:
        return None
    try:
        raw = [int(x, 16) for x in m.group(1).split(":")]
    except ValueError:
        return None
    if len(raw) < 2:
        return None
    return raw[0] >> 4, raw[0] & 0xF, raw[1:]


def encode_gateway_message(src: int, dst: int, payload: list[int]) -> str:
    """MQTT payload pro cec_gateway/send: ``src;dst;0xA0,0xA8,...``.

    Adresy vždy s 0x prefixem (gateway parser: dec nebo 0x hex; holé "E"
    se tichně neparsuje na 0 díky validaci ve firmware).
    """
    body = ",".join(f"0x{b:02X}" for b in payload)
    return f"0x{src:X};0x{dst:X};{body}"


def encode_pycec_message(src: int, dst: int, payload: list[int]) -> str:
    """Řádek protokolu pyCEC TCP bridge: ``05:A0:A8:82:00:A0\\r\\n``."""
    body = ":".join(f"{b:02X}" for b in payload)
    return f"{src << 4 | dst:02X}:{body}\r\n"
