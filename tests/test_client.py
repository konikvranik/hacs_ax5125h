"""Testy EzplayClient s fake transportem (bez homeassistant)."""

import asyncio
import unittest

from tests.test_ezplay import _load

ez = _load("ezplay")
transports = _load("transports")
client_mod = _load("client")

EzplayClient = client_mod.EzplayClient
EzplayTimeout = client_mod.EzplayTimeout
EzplayError = client_mod.EzplayError
CecTransport = transports.CecTransport

SB = 5
GW = 14


class FakeTransport(CecTransport):
    """Transport se skriptovanými odpověďmi; zaznamenává odeslané rámce."""

    def __init__(self, script=None):
        super().__init__()
        self.sent: list[tuple[int, int, list[int]]] = []
        self.script = script  # fn(src,dst,payload) -> (src,dst,payload)|None

    async def async_send(self, src, dst, payload):
        self.sent.append((src, dst, list(payload)))
        if self.script is not None:
            reply = self.script(src, dst, list(payload))
            if reply is not None:
                self._deliver(*reply)


def make_client(transport, **kw):
    c = EzplayClient(transport, session_delay=0, **kw)
    c.t_short = c.t_params = c.t_item = c.t_support = c.t_rtvr = 0.05
    return c



def default_script(src, dst, payload):
    """Plně responsivní soundbar: on (unmute 0x7A), A1, 0x91."""
    if ez.ezplay_sub(payload) == ez.SUB_GIVE_ALL_PARAMS:
        return (SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA1, 1, 1, 0x80, 0x82, 2])
    if ez.ezplay_sub(payload) == ez.SUB_GIVE_SUPPORT_FEATURES:
        return (SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0x91, 0x9F, 0x06])
    if payload[0] == ez.OP_GIVE_AUDIO_STATUS:
        return (SB, 0, [0x7A, 0x25])                       # unmute, vol 37
    if ez.ezplay_sub(payload) == ez.SUB_SET_ITEM_VALUE:
        return (SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA4,
                        payload[5], payload[6]])           # ack echo
    return None


class TestRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_happy_path(self):
        t = FakeTransport(default_script)
        c = make_client(t)
        await c.async_start()
        data = await c.async_refresh()
        self.assertEqual(data["eq"], 1)
        self.assertEqual(data["surr"], 1)
        self.assertEqual(data["bass"], 0)
        self.assertEqual(data["treble"], 2)
        self.assertEqual(data["dimmer"], 2)
        # power z refresh NEPOVODÍME (0x8F lže, mute bit kolísá i ve vypnutém
        # stavu) — řídí ho výhradně události (příkazy, 72:01, 90:01, push)
        self.assertNotIn("power", data)
        self.assertEqual(data["volume"], 37)
        self.assertFalse(data["mute"])
        self.assertEqual(data["support_mask"], 0x9F)
        self.assertEqual(data["support_items"], 6)
        # pořadí: 0x71 jde první (power/volume/mute z mute bitu; 0x8F se od
        # 0.5.0 neposílá — hlásí ON vždy i v deep standby), spoof musí
        # předcházet prvnímu EZPlay dotazu
        opcodes = [p[2][0] for p in t.sent]
        self.assertEqual(opcodes[0], ez.OP_GIVE_AUDIO_STATUS)
        self.assertNotIn(0x8F, opcodes)
        self.assertIn(0xA0, opcodes)
        # jen Hisense spoof (Roku spoof z RTVR handshake má OUI 8A:C7:2E)
        spoofs = [p for p in t.sent
                  if p[2][:4] == [0x87, 0xA8, 0x82, 0x00]]
        self.assertEqual(len(spoofs), 1)
        self.assertEqual(spoofs[0][0], 0)
        self.assertEqual(spoofs[0][1], 0xF)
        self.assertEqual(spoofs[0][2], [0x87, 0xA8, 0x82, 0x00])
        first_ezplay = next(
            i for i, p in enumerate(t.sent) if ez.ezplay_sub(p[2]) is not None
        )
        self.assertLess(t.sent.index(spoofs[0]), first_ezplay)

    async def test_refresh_timeout_raises(self):
        """0x71 mlčí v KAŽDÉM stavu (i deep standby odpovídá) — ticho =
        mrtvý transport -> EzplayTimeout."""
        t = FakeTransport(None)
        c = make_client(t)
        with self.assertRaises(EzplayTimeout):
            await c.async_refresh()
        sent_ops = [p[2][0] for p in t.sent]
        self.assertEqual(sent_ops, [ez.OP_GIVE_AUDIO_STATUS])

    async def test_refresh_no_traffic(self):
        """traffic=False (silent mód): NULA rámců — jakýkoliv dotaz baru
        brání v auto-standby (neprojde BYE do hlubokého spánku)."""
        t = FakeTransport(default_script)
        c = make_client(t)
        await c.async_start()
        await c.async_refresh(traffic=False)
        self.assertEqual(t.sent, [])

    async def test_activity_tracking(self):
        """Spontánní rámce ze SB a HA příkazy = aktivita ( rychlý polling),
        odpovědi na náš poll ne."""
        t = FakeTransport(default_script)
        c = make_client(t)
        await c.async_start()
        c.data.setdefault("last_activity", 0.0)
        await c.async_refresh()                       # poll odpovědi: bez změny
        self.assertEqual(c.data["last_activity"], 0.0)
        c._handle_frame(SB, 0, [0x7A, 30])            # spontánní push
        self.assertGreater(c.data["last_activity"], 0.0)
        c.data["last_activity"] = 0.0
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x01, 0x02])
        self.assertGreater(c.data["last_activity"], 0.0)  # A5 push
        c.data["last_activity"] = 0.0
        await c.async_set_volume(10)                  # HA příkaz
        self.assertGreater(c.data["last_activity"], 0.0)
        # odpovědi na cizí poll (gateway ping 0x8F → 90:00 flood každých 5 s)
        # aktivitou NEJSOU — jinak by rychlý poll nikdy neuvolnil (bug 0.6.1)
        c.data["last_activity"] = 0.0
        c._handle_frame(SB, 0, [0x90, 0x00])
        self.assertEqual(c.data["last_activity"], 0.0)
        c._handle_frame(SB, 15, [0x84, 0x30, 0x00, 0x05])
        self.assertEqual(c.data["last_activity"], 0.0)
        # TV ARC handshake smyčka (7E:01 každých ~5 s při SAM-on, #16) —
        # také šum, jinak by aktivní mód nikdy neuvolnil
        c._handle_frame(SB, 0, [0x7E, 0x01])
        self.assertEqual(c.data["last_activity"], 0.0)
        c._handle_frame(SB, 0, [0xA3, 0x21, 0x07])
        self.assertEqual(c.data["last_activity"], 0.0)

    async def test_event_power_signals(self):
        """Power = čistě eventový model: 90:01 → standby, spontánní 7A → on,
        72:01 SAM on → on + ARC. Odpověď 90:00 (gateway ping flood) power
        nezmění — CEC 'On' je na AX5125H bezvýznamná."""
        t = FakeTransport()
        c = make_client(t)
        c.data["power"] = "on"
        c._handle_frame(SB, 0, [0x90, 0x01])              # standby událost
        self.assertEqual(c.data["power"], "standby")
        c.data["power"] = "standby"
        c._handle_frame(SB, 0, [0x90, 0x00])              # ping odpověď: nechat
        self.assertEqual(c.data["power"], "standby")
        c._handle_frame(SB, 0, [0x7A, 25])                # spontánní 7A → on
        self.assertEqual(c.data["power"], "on")
        c.data["power"] = "standby"
        c._handle_frame(SB, 15, [0x72, 0x01])             # SAM on broadcast
        self.assertEqual(c.data["power"], "on")
        self.assertEqual(c.data["input"], "arc")

    async def test_wake_signature_activity(self):
        """50:8F (bar se sám ptá TV na její stav) = signatura probuzení
        (živě při IR-on) — aktivita, i když 0x8F vypadá jako poll opcode."""
        t = FakeTransport()
        c = make_client(t)
        c.data["last_activity"] = 0.0
        c._handle_frame(SB, 0, [0x8F])
        self.assertGreater(c.data["last_activity"], 0.0)


class TestWrites(unittest.IsolatedAsyncioTestCase):
    async def test_rx_less_set_item_optimistic(self):
        """hdmi_cec transport: bez rx se ack nečeká, stav optimistic."""
        t = FakeTransport(None)  # žádné odpovědi
        t.supports_rx = False
        c = make_client(t)
        self.assertFalse(c.rx_capable)
        ack = await c.async_set_item(ez.ITEM_EQ, 5)
        self.assertEqual(ack, 5)
        self.assertEqual(c.data["eq"], 5)
        # rámec odletěl (spoof + A2)
        sent_ops = [p[2][0] for p in t.sent]
        self.assertIn(0xA0, sent_ops)
        self.assertTrue(
            any(ez.ezplay_sub(p[2]) == ez.SUB_SET_ITEM_VALUE for p in t.sent)
        )

    async def test_rx_less_refresh_no_queries(self):
        """rx-less refresh neposílá žádné dotazy (žádné timeouty)."""
        t = FakeTransport(None)
        t.supports_rx = False
        c = make_client(t)
        data = await c.async_refresh()
        self.assertEqual(t.sent, [])
        self.assertIn("last_seen", data)

    async def test_set_item_ack(self):
        t = FakeTransport(default_script)
        c = make_client(t)
        await c.async_start()
        # bass −3: item 3, +0x80 kódování (0x7D); A4 echo = zapsaná hodnota
        ack = await c.async_set_item(ez.ITEM_BASS, ez.encode_bass_treble(-3))
        self.assertEqual(ack, 0x7D)
        self.assertEqual(c.data["bass"], -3)
        last = t.sent[-1]
        self.assertEqual(
            last, (0, SB, [0xA0, 0xA8, 0x82, 0x00, 0xA2, 0x03, 0x7D])
        )

    async def test_set_item_eq(self):
        t = FakeTransport(default_script)
        c = make_client(t)
        await c.async_start()
        ack = await c.async_set_item(ez.ITEM_EQ, 5)
        self.assertEqual(ack, 5)
        self.assertEqual(c.data["eq"], 5)

    async def test_set_item_timeout(self):
        t = FakeTransport(None)
        c = make_client(t)
        with self.assertRaises(EzplayTimeout):
            await c.async_set_item(ez.ITEM_EQ, 1)


class TestInputsAndPush(unittest.IsolatedAsyncioTestCase):
    async def test_input_arc(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_set_input("arc")
        self.assertEqual(t.sent[-1], (0, SB, [0x70, 0x00, 0x00]))
        self.assertEqual(c.data["input"], "arc")

    async def test_input_hdmi(self):
        t = FakeTransport()
        c = make_client(t, hdmi_pa=(0x31, 0x00))
        await c.async_set_input("hdmi")
        self.assertEqual(t.sent[-1], (GW, 0xF, [0x82, 0x31, 0x00]))
        self.assertEqual(c.data["input"], "hdmi")

    async def test_active_source_push_updates_input(self):
        t = FakeTransport()
        updates = []
        c = make_client(t, hdmi_pa=(0x31, 0x00))
        c.set_update_callback(lambda d: updates.append(dict(d)))
        # Kodi se stává aktivním zdrojem (broadcast od 1)
        c._handle_frame(1, 0xF, [0x82, 0x31, 0x00])
        self.assertEqual(c.data["input"], "hdmi")
        self.assertEqual(c.data["active_source"], "3.1.0.0")
        self.assertTrue(updates)
        # TV interní zdroj -> ARC
        c._handle_frame(0, 0xF, [0x82, 0x00, 0x00])
        self.assertEqual(c.data["input"], "arc")

    async def test_input_anchors(self):
        """Kotvy vstupu dle živých testů: Routing Change, Set Stream Path,
        SAM On broadcast i přímá odpověď Active Source adresovaná TV."""
        t = FakeTransport()
        c = make_client(t, hdmi_pa=(0x31, 0x00))
        # Routing Change 5F:80:30:00:31:00 (přechod na HDMI IN)
        c._handle_frame(SB, 0xF, [0x80, 0x30, 0x00, 0x31, 0x00])
        self.assertEqual(c.data["input"], "hdmi")
        # TV Set Stream Path 0F:86:31:00
        c._handle_frame(0, 0xF, [0x86, 0x31, 0x00])
        self.assertEqual(c.data["input"], "hdmi")
        # SAM On broadcast 5F:72:01 -> ARC
        c._handle_frame(SB, 0xF, [0x72, 0x01])
        self.assertEqual(c.data["input"], "arc")
        # SAM Off (72:00) je přechodný — stav nemění
        c._handle_frame(SB, 0xF, [0x72, 0x00])
        self.assertEqual(c.data["input"], "arc")
        # přímá odpověď Active Source na TV (ne broadcast)
        c._handle_frame(1, 0, [0x82, 0x31, 0x00])
        self.assertEqual(c.data["input"], "hdmi")
        self.assertEqual(c.data["active_source"], "3.1.0.0")

    async def test_audio_status_push(self):
        """Spontánní 0x7A push (IR/HA mute/volume) = bar živý → vždy on,
        i když je push mute=1 (ztlumený neznamená vypnutý!)."""
        t = FakeTransport()
        c = make_client(t)
        c.data["power"] = "on"
        c._handle_frame(SB, 0, [0x7A, 0x80 | 42])      # IR mute
        self.assertTrue(c.data["mute"])
        self.assertEqual(c.data["volume"], 42)
        self.assertEqual(c.data["power"], "on")
        c._handle_frame(SB, 0, [0x7A, 37])              # IR unmute
        self.assertFalse(c.data["mute"])
        self.assertEqual(c.data["power"], "on")

    async def test_a5_push_items(self):
        t = FakeTransport()
        c = make_client(t)
        # A5 inform: bass −3 v +0x80 kódování (živě: sweep 0x7B(−5)→0x81(+1))
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x03, 0x7D])
        self.assertEqual(c.data["bass"], -3)
        # A5: surround off → tri-state OFF (2); on → mód neznámý (poll
        # doplní) — po OFF předpoklad PURE (0)
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x02, 0x00])
        self.assertEqual(c.data["surr"], 2)
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x02, 0x01])
        self.assertEqual(c.data["surr"], 0)
        # A5: dimmer menu 2 (item5, raw; wire clamp 0–2)
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x05, 0x02])
        self.assertEqual(c.data["dimmer"], 2)
        # A5: EQ -> GAME (item1)
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x01, 0x05])
        self.assertEqual(c.data["eq"], 5)

    async def test_set_surround_and_dimmer(self):
        t = FakeTransport(default_script)
        c = make_client(t)
        await c.async_start()
        await c.async_set_dimmer(3)
        self.assertEqual(t.sent[-1], (0, SB, [0xA0, 0xA8, 0x82, 0x00,
                                              0xA2, 0x05, 0x03]))
        self.assertEqual(c.data["dimmer"], 3)

    async def test_rtvr_handshake_poll_set(self):
        """RTVR: handshake (announce→InitSession→AcceptSession), poll
        ReportState → surr tri-state, SetState zápis opt1."""
        def rtvr_script(src, dst, payload):
            if len(payload) >= 5 and payload[0] == 0xA0 \
                    and tuple(payload[1:4]) == (0x8A, 0xC7, 0x2E):
                sub = payload[4]
                if sub == 0x10:   # AnnouncePresence → InitSession
                    return (SB, 0, [0xA0, 0x8A, 0xC7, 0x2E, 0x11, 0x30,
                                    0x81, 0x24, 1, 2, 3, 4, 5, 6, 7, 8])
                if sub == 0x30:   # SetState → ReportState (echo opt1)
                    # holý poll (len 5) vrací aktuální stav, zápis (len 9)
                    # s opts (0x5D, 0xF?|mode, …) vrací aplikovanou hodnotu
                    mode = payload[6] & 0xF if len(payload) >= 7 else 1
                    return (SB, 0, [0xA0, 0x8A, 0xC7, 0x2E, 0x31, 0x5D,
                                    0xF0 | mode, 0x55, 0x0F])
            return None

        t = FakeTransport(rtvr_script)
        c = make_client(t)
        await c.async_start()
        surr = await c.rtvr_poll()
        self.assertEqual(surr, 1)              # opt1 = 1 → PRO
        self.assertEqual(c.data["surr"], 1)
        self.assertTrue(c._rtvr_ok)
        # handshake proběhl: Roku spoof + AnnouncePresence + AcceptSession
        subs = [p[2][4] for p in t.sent
                if len(p[2]) >= 5 and p[2][0] == 0xA0
                and tuple(p[2][1:4]) == (0x8A, 0xC7, 0x2E)]
        self.assertIn(0x10, subs)
        self.assertIn(0x12, subs)
        # zápis OFF (opt1=2) → ReportState potvrdí; F-maska mění jen opt1
        ok = await c.rtvr_set_surr(2)
        self.assertTrue(ok)
        self.assertEqual(c.data["surr"], 2)
        set_frames = [p[2] for p in t.sent if len(p[2]) == 9
                      and p[2][0] == 0xA0 and p[2][4] == 0x30]
        self.assertEqual(set_frames[-1],
                         [0xA0, 0x8A, 0xC7, 0x2E, 0x30, 0x5D, 0xF2, 0xFF,
                          0xFF])

    async def test_rtvr_no_response_suppressed(self):
        """Bez RTVR odpovědi: handshake selže a 5 min se neopakuje."""
        t = FakeTransport(None)
        c = make_client(t)
        await c.async_start()
        self.assertIsNone(await c.rtvr_poll())
        self.assertFalse(c._rtvr_ok)
        # druhé volání hned po sobě: handshake se neopakuje (throttle)
        sent_before = len(t.sent)
        self.assertIsNone(await c.rtvr_poll())
        self.assertEqual(len(t.sent), sent_before)

    async def test_ucp_push_updates_volume(self):
        t = FakeTransport()
        c = make_client(t)
        c.data["volume"] = 20
        # TV posílá UCP Volume Up na soundbar (0 -> 5: [0x44, 0x41])
        c._handle_frame(0, SB, [0x44, 0x41])
        self.assertEqual(c.data["volume"], 21)
        # TV posílá UCP Volume Down
        c._handle_frame(0, SB, [0x44, 0x42])
        self.assertEqual(c.data["volume"], 20)

    async def test_set_volume(self):
        """Absolutní hlasitost (EZPlay AC): frame, optimistic update,
        potvrzení 0x71 readbackem (default_script odpovídá vol=37)."""
        t = FakeTransport(default_script)
        c = make_client(t)
        await c.async_start()
        c.data["volume"] = 20
        await c.async_set_volume(20)
        acs = [p for p in t.sent if len(p[2]) >= 6
               and p[2][:5] == [0xA0, 0xA8, 0x82, 0x00, ez.SUB_SET_VOLUME]]
        self.assertEqual(len(acs), 1)
        self.assertEqual(acs[0], (0, SB, [0xA0, 0xA8, 0x82, 0x00, 0xAC, 20]))
        # optimistic hned, pak 0x71 readback (fake odpovídá 37) přebije
        self.assertEqual(c.data["volume"], 37)
        self.assertTrue(any(p[2][:1] == [0x71] for p in t.sent))

    async def test_set_volume_rx_less(self):
        """rx-less transport: jen optimistic aplikace, žádný 0x71 dotaz."""
        t = FakeTransport(None)
        t.supports_rx = False
        c = make_client(t)
        await c.async_start()
        c.data["volume"] = 20
        await c.async_set_volume(55)
        self.assertEqual(t.sent[-1], (0, SB, [0xA0, 0xA8, 0x82, 0x00,
                                              0xAC, 55]))
        self.assertEqual(c.data["volume"], 55)
        self.assertFalse(any(p[2][:1] == [0x71] for p in t.sent))

    async def test_volume_key(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_volume_key(ez.UCP_VOLUME_UP)
        self.assertEqual(t.sent[0], (0, SB, [0x44, 0x41]))
        self.assertEqual(t.sent[1], (0, SB, [0x45, 0x41]))

    async def test_power_on(self):
        """Power ON = UCP Power On (0x44 0x6D + 0x45 0x6D) — jediný dvakrát
        živě ověřený deep-standby wake; PASIVNÍ verifikace: probuzený bar
        broadcastuje 72:01 (SAM on, živě) → on zůstává. Žádné další dotazy
        (probuzený/uspávaný bar nesmí dostat traffic)."""
        def awake(src, dst, payload):
            if payload[0] == 0x44 and payload[1] == 0x6D:
                return (SB, 15, [0x72, 0x01])          # probuzený bar: SAM on
            return None

        t = FakeTransport(awake)
        c = make_client(t)
        await c.async_start()
        await c.async_power_on()
        # SAM terminate (odlepení ze zaseknutého stavu) + UCP pár
        self.assertEqual(t.sent[0], (0, SB, [0x70, 0x00, 0x00]))
        self.assertEqual(t.sent[1], (0, SB, [0x44, 0x6D]))
        self.assertEqual(t.sent[2], (0, SB, [0x45, 0x6D]))
        self.assertEqual(len(t.sent), 3)               # žádný traffic navíc
        self.assertEqual(c.data["power"], "on")
        self.assertEqual(c.data["input"], "arc")

    async def test_power_on_failed_reverts_to_standby(self):
        """Pasivní verifikace: žádná reakce baru → optimistické on se
        poctivě vrací na standby (žádné zaseknuté optimistické on)."""
        t = FakeTransport(None)                        # ticho — neprobudil se
        c = make_client(t)
        await c.async_start()
        await c.async_power_on()
        self.assertEqual(c.data["power"], "standby")
        self.assertEqual(len(t.sent), 3)               # SAM term. + UCP pár

    async def test_power_on_rx_less(self):
        """rx-less transport: jen UCP pár, optimisticky on."""
        t = FakeTransport(None)
        t.supports_rx = False
        c = make_client(t)
        await c.async_start()
        await c.async_power_on()
        self.assertEqual(t.sent, [(0, SB, [0x70, 0x00, 0x00]),
                                  (0, SB, [0x44, 0x6D]),
                                  (0, SB, [0x45, 0x6D])])
        self.assertEqual(c.data["power"], "on")

    async def test_source_pressed_cycles_inputs(self):
        """IR SOURCE stisk posouvá cyklus ARC→HDMI→OPT→AUX→USB→BT→ARC;
        neznámý stav startuje v ARC (default po bootu)."""
        t = FakeTransport()
        c = make_client(t)
        c.data["input"] = "hdmi"
        self.assertEqual(await c.async_source_pressed(), "opt")
        self.assertEqual(await c.async_source_pressed(), "aux")
        self.assertEqual(await c.async_source_pressed(), "usb")
        self.assertEqual(await c.async_source_pressed(), "bt")
        self.assertEqual(await c.async_source_pressed(), "arc")
        self.assertEqual(c.data["input"], "arc")
        c.data["input"] = None
        self.assertEqual(await c.async_source_pressed(), "hdmi")

    async def test_power_off(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_power_off()
        # SAM terminate (uvolní ARC handshake smyčku, #15) + Standby
        self.assertEqual(t.sent[0], (0, SB, [0x70, 0x00, 0x00]))
        self.assertEqual(t.sent[1], (0, SB, [0x36]))
        self.assertEqual(c.data["power"], "standby")

    async def test_power_off_settle_ignores_shutdown_choreography(self):
        """#20: hned po OFF bar broadcastne 72:01 (úvod ohlášení SAM role)
        a pushne 7A unmuted — přechodové rámce nesmí překlopit power/vstup."""
        t = FakeTransport()
        c = make_client(t)
        c.data["input"] = "hdmi"
        await c.async_power_off()
        # barova vypínací choreografie (živý záznam 00:35:54):
        c._handle_frame(SB, 0xF, [0x72, 0x01])          # 5F:72:01
        c._apply_audio((False, 17), is_reply=False)     # spontánní 7A unmuted
        self.assertEqual(c.data["power"], "standby")    # zůstává vypnutý!
        self.assertEqual(c.data["input"], "hdmi")       # neskočí v arc!
        # po skončení okna je 72:01 zase plnohodnotný signál
        c._off_settle_until = float("-inf")
        c._handle_frame(SB, 0xF, [0x72, 0x01])
        self.assertEqual(c.data["power"], "on")
        self.assertEqual(c.data["input"], "arc")

    async def test_power_off_double_suppressed(self):
        """#20: druhý OFF do 60 s neodesílá nic — TV by s barem přenegociovala
        ARC (C3/C0/C1 storm) a bar zůstal vzhůru."""
        t = FakeTransport()
        c = make_client(t)
        await c.async_power_off()
        self.assertEqual(len(t.sent), 2)
        await c.async_power_off()                       # hned znovu
        self.assertEqual(len(t.sent), 2)                # žádné nové rámce
        self.assertEqual(c.data["power"], "standby")
        # explicitní ON debounce zruší — off-on-off posílá vypnutí znovu
        t.sent.clear()
        await c.async_power_on()                        # (nebude probouzet —
        #   rx capable fake bez skriptu; jen kontrolujeme reset debouncu)
        await c.async_power_off()
        self.assertEqual([p[2] for p in t.sent][-2:], [[0x70, 0x00, 0x00],
                                                       [0x36]])

    async def test_set_mute(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_set_mute(True)
        self.assertTrue(c.data["mute"])
        self.assertEqual(t.sent[0], (0, SB, [0x44, 0x43]))
        self.assertEqual(t.sent[1], (0, SB, [0x45, 0x43]))


class TestSafety(unittest.IsolatedAsyncioTestCase):
    async def test_send_raw_blocked_a6(self):
        t = FakeTransport()
        c = make_client(t)
        with self.assertRaises(EzplayError):
            await c.async_send_raw(0, SB, ez.ezplay_frame(0xA6))

    async def test_send_raw_blocked_b0_b2(self):
        t = FakeTransport()
        c = make_client(t)
        for sub in (0xB0, 0xB2):
            with self.assertRaises(EzplayError):
                await c.async_send_raw(0, SB, ez.ezplay_frame(sub))

    async def test_send_raw_bare_a2_rejected(self):
        t = FakeTransport()
        c = make_client(t)
        with self.assertRaises(EzplayError):
            await c.async_send_raw(0, SB, [0xA0, 0xA8, 0x82, 0x00, 0xA2, 0x01])

    async def test_send_raw_ok(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_send_raw(0, SB, ez.ezplay_frame(ez.SUB_GET_ITEM_VALUE, [1]))
        self.assertEqual(len(t.sent), 1)


if __name__ == "__main__":
    unittest.main()
