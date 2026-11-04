"""Testy MQTT transportu — typy payloadu (regrese: HA doručuje str, ne bytes).

Živá zpráva z logu (pod HA 2026.10): callback padal
AttributeError: 'str' object has no attribute 'decode' → A1 nedošla
do parseru → validace hlásila "Soundbar neodpověděl", ačkoliv soundbar
odpověděl.
"""

import unittest

from tests.test_ezplay import _load

ez = _load("ezplay")
transports = _load("transports")

#: Reálná A1 odpověď soundbaru (EQ=MUSIC, SURR=PRO, bas=0, výšky=0, dim=0)
RX_A1 = (
    "50:A0:A8:82:00:A1:00:01:80:80:00"
    " => AudioSystem to TV: <Vendor Command With ID>[ID=a88200][.]"
)


class _FakeLoop:
    """call_soon_threadsafe -> spustí hned (simulace běžícího loopu)."""

    def __init__(self):
        self.scheduled = []

    def call_soon_threadsafe(self, cb, *args):
        self.scheduled.append((cb, args))
        cb(*args)


class _FakeHass:
    def __init__(self):
        self.loop = _FakeLoop()


class _Msg:
    """Minimalní náhrada MQTTMessage."""

    def __init__(self, payload):
        self.payload = payload


def _transport_with_sink():
    t = transports.MqttCecTransport(_FakeHass(), "cec_gateway")
    got = []
    t.set_frame_callback(lambda s, d, p: got.append((s, d, p)))
    return t, got


class TestOnMessagePayloadTypes(unittest.TestCase):
    def test_str_payload(self):
        """HA 2025.6+: payload je str (regrese živého selhání)."""
        t, got = _transport_with_sink()
        t._on_message(_Msg(RX_A1))
        self.assertEqual(len(got), 1)
        self.assertEqual(len(t._hass.loop.scheduled), 1)
        src, dst, payload = got[0]
        self.assertEqual(src, 5)
        self.assertEqual(dst, 0)
        self.assertEqual(payload[:4], [0xA0, 0xA8, 0x82, 0x00])
        self.assertEqual(payload[4], 0xA1)
        params = ez.parse_all_params(payload)
        self.assertEqual(
            params, {"eq": 0, "surr": 1, "bass": 0, "treble": 0, "dimmer": 0}
        )

    def test_bytes_payload(self):
        """Starší HA: payload je bytes."""
        t, got = _transport_with_sink()
        t._on_message(_Msg(RX_A1.encode("utf-8")))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], 5)

    def test_bytearray_payload(self):
        """ReceivePayloadType zahrnuje i bytearray."""
        t, got = _transport_with_sink()
        t._on_message(_Msg(bytearray(RX_A1.encode("utf-8"))))
        self.assertEqual(len(got), 1)

    def test_garbage_ignored(self):
        """Ne-rámec se tiše zahodí."""
        t, got = _transport_with_sink()
        t._on_message(_Msg("hello world"))
        self.assertEqual(got, [])

    def test_off_loop_delivery_lands_on_loop(self):
        """Rx z paho vlákna musí proběhnout V event loopu.

        Živá regrese 0.2.3: HA mqtt doručuje callbacky mimo loop →
        coordinator.async_set_updated_data → async_write_ha_state
        z ne-loop vlákna → RuntimeError + neaktualizující se entity.
        """
        import asyncio
        import threading
        import types

        async def main():
            loop = asyncio.get_running_loop()
            hass = types.SimpleNamespace(loop=loop)
            t = transports.MqttCecTransport(hass, "cec_gateway")
            seen = []
            t.set_frame_callback(
                lambda s, d, p: seen.append(
                    (s, d, threading.current_thread() is threading.main_thread())
                )
            )
            # simulace paho vlákna: callback spuštěný v executoru
            await asyncio.to_thread(t._on_message, _Msg(RX_A1))
            await asyncio.sleep(0.05)  # ať call_soon_threadsafe proběhne
            return seen

        seen = asyncio.run(main())
        self.assertEqual(len(seen), 1, seen)
        src, dst, in_loop = seen[0]
        self.assertEqual((src, dst), (5, 0))
        self.assertTrue(in_loop, "rx doručeno mimo event loop")


if __name__ == "__main__":
    unittest.main()
