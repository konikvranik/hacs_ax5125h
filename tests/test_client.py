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
    c.t_short = c.t_params = c.t_item = c.t_support = 0.05
    return c



def default_script(src, dst, payload):
    """Plně responsivní soundbar: on, A1, 0x91, 0x7D."""
    if payload[0] == 0x8F:
        return (SB, 0, [0x90, 0x00])                       # on
    if ez.ezplay_sub(payload) == ez.SUB_GIVE_ALL_PARAMS:
        return (SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA1, 1, 1, 0x80, 0x82, 2])
    if ez.ezplay_sub(payload) == ez.SUB_GIVE_SUPPORT_FEATURES:
        return (SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0x91, 0x9F, 0x06])
    if payload[0] == ez.OP_GIVE_AUDIO_STATUS:
        return (SB, 0, [0x7D, 0x25])                       # volume 37
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
        self.assertEqual(data["power"], "on")
        self.assertEqual(data["volume"], 37)
        self.assertFalse(data["mute"])
        self.assertEqual(data["support_mask"], 0x9F)
        self.assertEqual(data["support_items"], 6)
        # pořadí: 0x8F jde první (standardní CEC, session nepotřebuje),
        # spoof (0->F vendor ID) musí předcházet prvnímu EZPlay dotazu
        opcodes = [p[2][0] for p in t.sent]
        self.assertEqual(opcodes[0], 0x8F)
        self.assertIn(0xA0, opcodes)
        spoofs = [p for p in t.sent if p[2][:1] == [0x87]]
        self.assertEqual(len(spoofs), 1)
        self.assertEqual(spoofs[0][0], 0)
        self.assertEqual(spoofs[0][1], 0xF)
        self.assertEqual(spoofs[0][2], [0x87, 0xA8, 0x82, 0x00])
        first_ezplay = next(
            i for i, p in enumerate(t.sent) if ez.ezplay_sub(p[2]) is not None
        )
        self.assertLess(t.sent.index(spoofs[0]), first_ezplay)

    async def test_refresh_standby_skips_params(self):
        def standby(src, dst, payload):
            if payload[0] == 0x8F:
                return (SB, 0, [0x90, 0x01])
            return None

        t = FakeTransport(standby)
        c = make_client(t)
        await c.async_start()
        data = await c.async_refresh()
        self.assertEqual(data["power"], "standby")
        sent_ops = [p[2][0] for p in t.sent]
        self.assertNotIn(ez.OP_GIVE_AUDIO_STATUS, sent_ops)
        # žádný A0 all-params dotaz
        self.assertFalse(
            any(ez.ezplay_sub(p[2]) == ez.SUB_GIVE_ALL_PARAMS for p in t.sent)
        )

    async def test_refresh_timeout_raises(self):
        t = FakeTransport(None)
        c = make_client(t)
        with self.assertRaises(EzplayTimeout):
            await c.async_refresh()
        # retry: alespoň 2 all-params pokusy + 2 spoofy
        spoofs = [p for p in t.sent if p[2][:1] == [0x87]]
        self.assertGreaterEqual(len(spoofs), 2)


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
        ack = await c.async_set_item(ez.ITEM_BASS, -3)
        self.assertEqual(ack, 0xFD)
        self.assertEqual(c.data["bass"], -3)
        last = t.sent[-1]
        self.assertEqual(
            last, (0, SB, [0xA0, 0xA8, 0x82, 0x00, 0xA2, 0x02, 0xFD])
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

    async def test_a5_push_bass_offset_0x7b(self):
        t = FakeTransport()
        c = make_client(t)
        # A5 inform: bass +3 v offsetu +0x7B = 0x7E
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x02, 0x7E])
        self.assertEqual(c.data["bass"], 3)
        # A5: EQ -> GAME
        c._handle_frame(SB, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA5, 0x01, 0x05])
        self.assertEqual(c.data["eq"], 5)

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

    async def test_volume_key(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_volume_key(ez.UCP_VOLUME_UP)
        self.assertEqual(t.sent[0], (0, SB, [0x44, 0x41]))
        self.assertEqual(t.sent[1], (0, SB, [0x45, 0x41]))

    async def test_power_on(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_power_on()
        # sam request + ucp power on
        self.assertEqual(t.sent[0], (0, SB, [0x70, 0x00, 0x00]))
        self.assertEqual(t.sent[1], (0, SB, [0x44, 0x6D]))
        self.assertEqual(t.sent[2], (0, SB, [0x45, 0x6D]))
        self.assertEqual(c.data["power"], "on")

    async def test_power_off(self):
        t = FakeTransport()
        c = make_client(t)
        await c.async_power_off()
        self.assertEqual(t.sent[0], (0, SB, [0x36]))
        self.assertEqual(c.data["power"], "standby")

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
