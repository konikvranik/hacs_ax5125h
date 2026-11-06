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
| Bass / Treble | number (slider) | −5 … +5 | ✅ |
| Surround | select | **PURE / PRO / OFF** (tri-state read & written via Roku RTVR — EZPlay item2 is binary-only) | ✅ |
| Display brightness (dimmer) | number (slider) | 0–3 (internal 1–4; 5+ corrupts the menu — guarded) | ✅ |
| Power | switch | power on (**Set Stream Path broadcast** — wakes even from deep standby, live-verified) / standby (CEC 0x36) | ✅ |
| **Media player** | media_player | **`volume_set` for voice assistant** (absolute 0–100 %), volume up/down, mute, power, source select | ✅ |
| Volume | number (slider) | 0–100 % **absolute set** (EZPlay Set_Volume) + IR/UCP changes tracked (0x71→0x7A readback/push) | ✅ |
| Mute | switch | mute state (read via 0x7A + UCP 0x43 toggling) | ✅ |
| Volume +/− | button | CEC UCP (0x44/0x45) | ✅ |
| **Input → ARC** | select | System Audio Mode Request as the TV (`0;5;0x70,0x00,0x00`) | ✅ |
| **Input → HDMI IN** | select | Active Source as the gateway (`0xE;0xF;0x82,0x31,0x00`) | ✅ |
| Push changes from menu/remote | — | EZPlay `A5` inform → instant entity updates | ✅ |
| EZPlay support features | sensor (diagnostic) | `91 9F 06` | ✅ |
| Active source (bus) | sensor (diagnostic) | 0x82 broadcasts | ✅ |
| Services | `send_raw`, `volume_up/down`, `mute_toggle`, `source_pressed` | `source_pressed` = IR SOURCE press tracking; guard rails against destructive subopcodes | ✅ |

Neural:X / Virtual:X and the OPT/AUX/USB/BT inputs are **not** reachable over
CEC (definitively verified — see the research notes below).

## Transports (the path to the CEC bus)

| Transport | Configuration | Notes |
|---|---|---|
| **ESPHome gateway (MQTT)** ⭐ | topic prefix (`cec_gateway`) | Recommended. Requires an [hdmi-cec-gateway](https://github.com/Palakis/esphome-native-hdmi-cec) with MQTT; full functionality incl. spoofed source addresses and received frames |
| ESPHome gateway (native API) | service `esphome.<node>_cec_send` | Without a reflash it can only send frames from the gateway (0xE → HDMI switching); with the added `cec_send_src` action (see below) fully functional |
| **HDMI-CEC integration** | `hdmi_cec:` in your configuration.yaml | Uses the built-in [HDMI-CEC integration](https://www.home-assistant.io/integrations/hdmi_cec/) exactly as you configure it — local adapter or remote pyCEC host. See the limitation below |

All transports can send frames with an **arbitrary source address** — that is
the key: EZPlay commands must come from the TV (address 0), while switching to
HDMI IN must come from a switch-role device on the eARC side (0xE = gateway).

### HDMI-CEC integration limitation (read before choosing)

The built-in `hdmi_cec` integration can **send** arbitrary frames
(`hdmi_cec.send_command` with `raw`) but does **not** report received frames
(it never fires the `cec_command_received` event). With this transport:

- **all commands work** — EQ/bass/treble/surround/dimmer changes, input
  switching, volume keys (they are fire-and-forget)
- **vendor state is optimistic** — entities show the last value you set; the
  integration cannot poll the soundbar (no responses arrive)
- **power state** is read from the hdmi_cec switch entity of the soundbar
  (e.g. `switch.hdmi_5`) — set it in the config flow

For full two-way functionality (real status, A5 push updates from the
device's own menu/remote) use one of the ESPHome gateway transports.

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

- **Poll interval** (30 s; min 10 s) — each poll = audio status (power/volume/
  mute), then (when on) the all-params query (A0→A1); push changes (A5) arrive
  out-of-band
- **Physical address of HDMI IN** (`3.1.0.0`) — where Active Source switches to
  **and which port the power-ON Set Stream Path targets**
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
- bass/treble are written in +0x80 wire encoding (item 3/4); out-of-range
  writes corrupt NVRAM (fixed in 0.3.0 — earlier builds wrote shifted item
  ids; repair via RTVR SetState + one official IR change)

## Power ON/OFF and power state (0.7.0 — event-driven, live-verified)

- **OFF** = CEC `<Standby>` (0x36), works reliably.
- **ON** = **UCP Power On** (`0x44 0x6D` + `0x45 0x6D`) — the only frame
  live-verified to wake the bar from **deep standby** (twice verified: the
  waking bar broadcasts SAM-on and pushes audio status). Verification is
  **passive**: if the bar emits no frame within 8 s, the wake failed and the
  state honestly reverts to *standby* — no extra queries are sent (any query
  prevents the bar from entering its auto-standby "BYE" → deep sleep).
- **Power state is event-driven** — this device *lies* over CEC: `Report
  Power Status` says *On* in every state and the audio-status mute bit
  fluctuates even while off (both live-verified). So the integration does
  **not poll for state at all**. Instead:
  - HA commands set the state optimistically, and after power-off the
    integration **silences all traffic** toward the bar so it can go
    *BYE* → deep sleep → the red LED turns off like it should
  - events from the bar update the state: SAM-on broadcast (wake/input),
    spontaneous audio-status push (IR volume/mute), `90:01` standby report
  - within the active window (10 min after the last command/push) the
    integration polls volume/EQ normally; after it, **zero queries** until
    the next event
- Known limits: turning the bar off by IR (not via HA) is picked up only if
  the bar emits a standby report; the state is *unknown* after an HA restart
  until the first event or command arrives.

## Limitations / notes

- Switching to HDMI IN is done via an Active Source broadcast as the gateway —
  the switch can trigger a transient Standby report from the soundbar, which
  Kodi (libcec) sometimes reacts to by going to sleep
  (`standby_pc_on_tv_standby`). Existing behaviour, see the docs.
- Input state: ARC/HDMI are detected via CEC anchors (SAM On broadcast,
  Routing Change/Active Source/Set Stream Path). OPT/AUX/USB/BT emit **no**
  CEC frames at all — their state is tracked by counting IR SOURCE presses
  (cycle ARC→HDMI→OPT→AUX→USB→BT): call the `source_pressed` service from an
  automation triggered by your IR receiver. Selecting those inputs in the
  UI is a **declaration** (the state is recorded and survives restarts, no
  CEC frame goes out — the device cannot be switched there remotely); ARC
  and HDMI are switched via real CEC commands.
- Surround mode: the EZPlay layer only carries an on/off flag; the PURE / PRO
  distinction is read and written through the **Roku RTVR** protocol
  (`SetState`/`ReportState`, option 1). The integration impersonates the Roku
  TV for the handshake; without RTVR support the select keeps the EZPlay
  on/off fallback.
- Volume: absolute set goes through the EZPlay `AC Set_Volume` subopcode
  (raw 0–100, live-verified). It has **no ack** — the write is confirmed by
  the 0x7A push / a follow-up 0x71 readback; values > 100 are clamped to 100
  by the firmware. Relative control (UCP ± buttons, mute toggle) still works.
- After a reboot the soundbar ignores vendor commands for ~30 s (standard CEC
  init).

## Brand icon

Since HA 2026.3 the integration ships its own icon in
`custom_components/ax5125h/brand/` (Hisense wordmark, light + dark variants).
Home Assistant picks it up automatically — no configuration needed. Older HA
versions show the generic placeholder icon instead.

## Research

The protocol comes from reverse engineering the AX3120G firmware plus live
tests on the AX5125H (November 2025). Full documentation (opcodes, value
tables, the RTVR layer, dead ends): repository
`home_assistant_config/esphome/docs/soundbar-ax5125h/`.

## License

[MIT](LICENSE)
