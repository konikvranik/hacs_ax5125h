# Hisense AX5125H Soundbar (HACS)

**[English](README.md)** | [Čeština](README.cs.md)

Home Assistant custom integration for the **Hisense AX5125H** soundbar
(5.1.2, eARC), controlled over **HDMI-CEC** using the reverse-engineered
vendor protocol **EZPlay** (Hisense, OUI `A8:82:00`). Works even without the
original Hisense/Roku TV — the integration impersonates the TV itself
(vendor ID spoofing over CEC).

![protocol](https://img.shields.io/badge/protocol-EZPlay-blue) ![CEC](https://img.shields.io/badge/transport-HDMI--CEC-green) ![i18n](https://img.shields.io/badge/UI-EN%20%2B%20CZ-brightgreen)

## Features

| Entity | Type | Range | Verified live |
|---|---|---|---|
| EQ | select | MUSIC / MOVIE / NEWS / SPORT / NIGHT / GAME / AI EQ | ✅ |
| Bass / Treble | number | −5 … +5 | ✅ |
| Surround | select | PURE / PRO / OFF | ✅ |
| Display brightness (dimmer) | select | 0–3 (internal 1–4; 5+ corrupts the menu — guarded) | ✅ |
| Power | sensor | on / standby / turning on/off (CEC 0x8F→0x90) | ✅ |
| Volume + mute | sensor | 0–100 % (CEC 0x71→0x7D) | ❓ best-effort — our unit ignores 0x71, sensor stays unknown (buttons still work) |
| Volume +/−, Mute | button | CEC UCP (0x44/0x45) | ✅ |
| **Input → ARC** | select | System Audio Mode Request as the TV (`0;5;0x70,0x00,0x00`) | ✅ |
| **Input → HDMI IN** | select | Active Source as the gateway (`0xE;0xF;0x82,0x31,0x00`) | ✅ |
| Push changes from menu/remote | — | EZPlay `A5` inform → instant entity updates | ✅ |
| EZPlay support features | sensor (diagnostic) | `91 9F 06` | ✅ |
| Active source (bus) | sensor (diagnostic) | 0x82 broadcasts | ✅ |
| Services | `send_raw`, `volume_up/down`, `mute_toggle` | guard rails against destructive subopcodes | ✅ |

Neural:X / Virtual:X and the OPT/AUX/USB/BT inputs are **not** reachable over
CEC (definitively verified — see the research notes below).

## Transports (the path to the CEC bus)

| Transport | Configuration | Notes |
|---|---|---|
| **ESPHome gateway (MQTT)** ⭐ | topic prefix (`cec_gateway`) | Recommended. Requires an [hdmi-cec-gateway](https://github.com/Palakis/esphome-native-hdmi-cec) with MQTT; full functionality incl. spoofed source addresses |
| ESPHome gateway (native API) | service `esphome.<node>_cec_send` | Without a reflash it can only send frames from the gateway (0xE → HDMI switching); with the added `cec_send_src` action (see below) fully functional |
| **pyCEC TCP bridge** | host + port 9526 | [pyCEC](https://github.com/konikvranik/pycec) server (`python3 -m pycec`) on a machine with a CEC adapter; HA connects as a plain client — no libcec needed inside HA |
| libcec local | system module `cec` | libcec python bindings directly inside HA (HA Core in a venv/container with libcec installed; **not** HAOS) |

All transports can send frames with an **arbitrary source address** — that is
the key: EZPlay commands must come from the TV (address 0), while switching to
HDMI IN must come from a switch-role device on the eARC side (0xE = gateway).

### ESPHome gateway notes

1. **CEC Bus Log Enabled** — the gateway only publishes `cec_gateway/rx` (and
   fires the `esphome.cec_rx` event) while the bus log switch is on. The
   integration can **enable this switch automatically** — it is offered in the
   config flow.
2. **`cec_send_src`** (native API transport only) — the stock `cec_send`
   action always sends from the gateway address. Add an action with a `source`
   parameter to the gateway config (see the gateway docs), and the native
   transport can send EZPlay commands as the TV too.

## Installation (HACS)

1. HACS → ⋮ → *Custom repositories* → `https://github.com/konikvranik/hacs_ax5125h`,
   category **Integration**
2. HACS → Integrations → **Hisense AX5125H Soundbar** → Download
3. Restart HA
4. Settings → Devices & Services → **Add Integration** → *Hisense AX5125H
   Soundbar* → pick a transport → handshake validation → done

Manual: copy `custom_components/ax5125h/` into `config/custom_components/`.

The UI is localized (English + Czech); HA picks the language automatically.

## Configuration

The config flow asks for transport parameters, then for shared CEC parameters
(defaults for the usual topology in parentheses):

- **Poll interval** (30 s; min 10 s) — each poll = power status, the
  all-params query (A0→A1) and audio status; push changes (A5) arrive
  out-of-band
- **Physical address of HDMI IN** (`3.1.0.0`) — where Active Source switches to
- **TV address** (0), **soundbar** (5), **gateway** (0xE = 14)

Change later via *Configure* on the integration entry.

## How it works (EZPlay in a nutshell)

```
[src:dst] A0 A8 82 00 <subopcode> [payload...]     ← vendor frame (sender: TV)
TV→F:     87 A8 82 00                              ← TV vendor ID spoof (session ~60 s)
TV→SB:    A0 A8 82 00 A0                           ← Give_All_Params → A1 report
TV→SB:    A0 A8 82 00 A2 <item> <val>              ← Set_Item_Value → A4 ack
SB→TV:    A0 A8 82 00 A5 <item> <val>              ← push on menu/remote change
```

Items: 1=EQ (0–6), 2=BASS, 3=TREBLE (−5…+5), 4=DIMMER (1–4), 5=SURROUND (0–2).
Readout: A1 = `[EQ][SURR][BASS+0x80][TRE+0x80][DIM]` (most reliable).

**Brick protection** (things the integration refuses to do):

- subopcodes `A6` (Reset_All_Params) and `B0`/`B2` (Give_Item_Range —
  destructive on the AX5125H) cannot be sent, not even via `send_raw`
- a bare `A2 <item>` without a value (it is not a GET — it writes garbage)
  is rejected
- dimmer stays within the safe 0–3 range (5+ corrupts the menu)
- bass/treble are only written within −5…+5 (nibble 11+ corrupts NVRAM via RTVR)

## Limitations / notes

- Switching to HDMI IN is done via an Active Source broadcast as the gateway —
  the switch can trigger a transient Standby report from the soundbar, which
  Kodi (libcec) sometimes reacts to by going to sleep
  (`standby_pc_on_tv_standby`). Existing behaviour, see the docs.
- The current input is inferred (Active Source broadcasts + optimistic update
  after a command) — the soundbar does not report its input by itself.
- Volume is relative only (UCP ± / mute) plus the 0x7D readout; the EZPlay
  `AC Set_Volume` opcode has an unverified value format and is deliberately
  unused.
- After a reboot the soundbar ignores vendor commands for ~30 s (standard CEC
  init).

## Research

The protocol comes from reverse engineering the AX3120G firmware plus live
tests on the AX5125H (November 2025). Full documentation (opcodes, value
tables, the RTVR layer, dead ends): repository
`home_assistant_config/esphome/docs/soundbar-ax5125h/`.

## License

[MIT](LICENSE)
