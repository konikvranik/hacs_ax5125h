# Hisense AX5125H Soundbar (HACS)

[English](README.md) | **[Čeština](README.cs.md)**

Home Assistant custom integrace pro soundbar **Hisense AX5125H** (5.1.2, eARC)
ovládaný přes **HDMI-CEC** pomocí reverzně inženýrenského vendor protokolu
**EZPlay** (Hisense, OUI `A8:82:00`). Funguje i bez originální Hisense/Roku TV —
integrace se za TV „vydává“ sama (podvržení vendor ID přes CEC).

![protokol](https://img.shields.io/badge/protocol-EZPlay-blue) ![CEC](https://img.shields.io/badge/transport-HDMI--CEC-green) ![i18n](https://img.shields.io/badge/UI-EN%20%2B%20CZ-brightgreen)

## Co umí

| Entita | Typ | Rozsah | Živě ověřeno |
|---|---|---|---|
| EQ | select | MUSIC / MOVIE / NEWS / SPORT / NIGHT / GAME / AI EQ | ✅ |
| Bass / Výšky | number | −5 … +5 | ✅ |
| Surround | select | PURE / PRO / OFF | ✅ |
| Jas displeje (dimmer) | select | 0–3 (interně 1–4; 5+ korrumpuje menu — pojistka v integraci) | ✅ |
| Napájení | sensor | on / standby / turning on/off (CEC 0x8F→0x90) | ✅ |
| Hlasitost + mute | sensor | 0–100 % (CEC 0x71→0x7D) | ❓ best-effort — náš kus na 0x71 mlčí, senzor zůstává unknown (tlačítka fungují) |
| Hlasitost +/−, Mute | button | CEC UCP (0x44/0x45) | ✅ |
| **Vstup → ARC** | select | SAM Request jako TV (`0;5;0x70,0x00,0x00`) | ✅ |
| **Vstup → HDMI IN** | select | Active Source za gateway (`0xE;0xF;0x82,0x31,0x00`) | ✅ |
| Push změny z menu/ovladače | — | EZPlay `A5` inform → okamžitá aktualizace entit | ✅ |
| EZPlay support features | sensor (diagnostika) | `91 9F 06` | ✅ |
| Active source (sběrnice) | sensor (diagnostika) | 0x82 broadcasty | ✅ |
| Služby | `send_raw`, `volume_up/down`, `mute_toggle` | pojistka proti destruktivním subopcodům | ✅ |

Neural:X / Virtual:X a vstupy OPT/AUX/USB/BT **nejsou** přes CEC dostupné
(definitivně ověřeno — viz výzkumná dokumentace níže).

## Transporty (cesta k CEC sběrnici)

| Transport | Konfigurace | Poznámky |
|---|---|---|
| **ESPHome gateway (MQTT)** ⭐ | topic prefix (`cec_gateway`) | Doporučeno. Vyžaduje [hdmi-cec-gateway](https://github.com/Palakis/esphome-native-hdmi-cec) s MQTT; plná funkcionalita včetně podvržení zdrojových adres i přijímaných rámců |
| ESPHome gateway (native API) | služba `esphome.<node>_cec_send` | Bez reflashe umí jen rámce od gateway (0xE → HDMI switch); s přidanou akcí `cec_send_src` (viz níže) plná funkčnost |
| **Integrace HDMI-CEC** | `hdmi_cec:` v configuration.yaml | Použije vestavěnou [integraci HDMI-CEC](https://www.home-assistant.io/integrations/hdmi_cec/) přesně tak, jak si ji nastavíte — lokální adapter nebo vzdálený pyCEC host. Viz omezení níže |

Všechny transporty umí odesílat rámce s **libovolnou zdrojovou adresou** — to je
klíčové: EZPlay příkazy musí jít od TV (adresa 0), přepnutí na HDMI IN naopak od
switch-role zařízení z eARC strany (0xE = gateway).

### Omezení integrace HDMI-CEC (přečtěte před volbou)

Vestavěná integrace `hdmi_cec` umí rámce **posílat** (`hdmi_cec.send_command`
s `raw`), ale přijaté rámce **nehlásí** (event `cec_command_received`
nevysílá). S tímto transportem:

- **všechny příkazy fungují** — změny EQ/basu/výšek/surroundu/dimmeru,
  přepínání vstupů, hlasitost (vše fire-and-forget)
- **vendor stavy jsou optimistické** — entity ukazují poslední vámi nastavenou
  hodnotu; soundbar nelze pollit (odpovědi nedorazí)
- **stav napájení** se čte ze switch entity soundbaru v hdmi_cec (např.
  `switch.hdmi_5`) — nastavte ji v config flow

Pro plnou obousměrnou funkcionalitu (skutečný stav, A5 push změny z vlastního
menu/ovladače) použijte jeden z ESPHome gateway transportů.

### Poznámka k ESPHome gatewayi

1. **CEC Bus Log Enabled** — gateway publikuje `cec_gateway/rx` (a vysílá event
   `esphome.cec_rx`) jen se zapnutým přepínačem bus logu. Integrace umí tento
   switch **zapnout automaticky** — v config flow se nabídne k výběru.
2. **`cec_send_src`** (jen native API transport) — výchozí akce `cec_send`
   odesílá vždy od adresy gateway. Přidejte do configu gatewaye akci s parametrem
   `source`, pak native transport umí i EZPlay příkazy za TV.

## Instalace (HACS)

1. HACS → ⋮ → *Custom repositories* → `https://github.com/konikvranik/hacs_ax5125h`,
   kategorie **Integration**
2. HACS → Integrations → **Hisense AX5125H Soundbar** → Download
3. Restart HA
4. Nastavení → Zařízení a služby → **Přidat integraci** → *Hisense AX5125H
   Soundbar* → zvolit transport → validace handshake → hotovo

Ručně: zkopírujte `custom_components/ax5125h/` do `config/custom_components/`.

UI je lokalizované (angličtina + čeština); HA jazyk vybere automaticky.

## Konfigurace

Config flow se zeptá na parametry transportu, pak na společné parametry CEC
(v závorkách výchozí hodnoty pro typickou topologii):

- **Poll interval** (30 s; min 10 s) — každý poll = power status, all-params
  dotaz (A0→A1) a audio status; push změny (A5) chodí mimo poll
- **Fyzická adresa HDMI IN** (`3.1.0.0`) — kam přepínat Active Source
- **Adresa TV** (0), **soundbaru** (5), **gatewaye** (0xE = 14)

Změna později přes *Konfigurovat* u záznamu integrace.

## Jak to funguje (EZPlay v kostce)

```
[src:dst] A0 A8 82 00 <subopcode> [payload...]     ← vendor rámec (odesílatel TV)
TV→F:     87 A8 82 00                              ← podvrh TV vendor ID (session ~60 s)
TV→SB:    A0 A8 82 00 A0                           ← Give_All_Params → A1 report
TV→SB:    A0 A8 82 00 A2 <item> <val>              ← Set_Item_Value → A4 ack
SB→TV:    A0 A8 82 00 A5 <item> <val>              ← push při změně z menu/ovladače
```

Items: 1=EQ (0–6), 2=BASS, 3=TREBLE (−5…+5), 4=DIMMER (1–4), 5=SURROUND (0–2).
Čtení: A1 = `[EQ][SURR][BASS+0x80][TRE+0x80][DIM]` (nejspolehlivější).

**Pojistky proti bricknutí** (věci, které integrace odmítá udělat):

- subopcody `A6` (Reset_All_Params), `B0`/`B2` (Give_Item_Range — na AX5125H
  destruktivní) nelze odeslat ani přes službu `send_raw`
- holé `A2 <item>` bez hodnoty (není to GET — zapíše garbage) je odmítnuto
- dimmer zůstává v bezpečném rozsahu 0–3 (5+ korrumpuje menu)
- bass/treble se zapisují jen v −5…+5 (nibble 11+ u RTVR korrumpuje NVRAM)

## Omezení / poznámky

- Vstup HDMI IN se přepíná Active Source broadcastem za gateway — přepnutí
  může vyvolat přechodový Standby report soundbaru, na který Kodi (libcec)
  občas reaguje uspáním (`standby_pc_on_tv_standby`). Existující chování, viz
  dokumentace.
- Aktuální vstup je inference (Active Source broadcasty + optimistická
  aktualizace po příkazu) — soundbar svůj vstup sám nehlásí.
- Hlasitost je jen relativní (UCP ± / mute) + čtení 0x7D; EZPlay `AC
  Set_Volume` má neověřený formát hodnoty a záměrně se nepoužívá.
- Soundbar po rebootu ~30 s ignoruje vendor příkazy (standardní CEC init).

## Výzkum

Protokol vznikl reverzní analýzou firmware AX3120G + živými testy na AX5125H
(listopad 2025). Kompletní dokumentace (opcody, tabulky hodnot, RTVR vrstva,
mrtvé cesty): repozitář `home_assistant_config/esphome/docs/soundbar-ax5125h/`.

## Licence

[MIT](LICENSE)
