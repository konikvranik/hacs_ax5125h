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


class _Msg:
    """Minimalní náhrada MQTTMessage."""

    def __init__(self, payload):
        self.payload = payload


def _transport_with_sink():
    t = transports.MqttCecTransport(None, "cec_gateway")
    got = []
    t.set_frame_callback(lambda s, d, p: got.append((s, d, p)))
    return t, got


class TestOnMessagePayloadTypes(unittest.TestCase):
    def test_str_payload(self):
        """HA 2025.6+: payload je str (regrese živého selhání)."""
        t, got = _transport_with_sink()
        t._on_message(_Msg(RX_A1))
        self.assertEqual(len(got), 1)
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


if __name__ == "__main__":
    unittest.main()
