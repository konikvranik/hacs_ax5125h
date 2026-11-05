"""Testy EZPlay codeců (čisté funkce, bez homeassistant)."""

import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ax5125h"
PKG = "ax5125h"


def _load(name: str):
    """Načte modul balíku bez spuštění __init__.py (žádné HA importy)."""
    if PKG not in sys.modules:
        stub = types.ModuleType(PKG)
        stub.__path__ = [str(ROOT)]
        sys.modules[PKG] = stub
    if f"{PKG}.{name}" in sys.modules:
        return sys.modules[f"{PKG}.{name}"]
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.{name}", ROOT / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


ez = _load("ezplay")


class TestFrameBuilders(unittest.TestCase):
    def test_vendor_spoof(self):
        self.assertEqual(ez.vendor_spoof_frame(), [0x87, 0xA8, 0x82, 0x00])

    def test_ezplay_frame(self):
        self.assertEqual(
            ez.ezplay_frame(0xA0), [0xA0, 0xA8, 0x82, 0x00, 0xA0]
        )
        self.assertEqual(
            ez.set_item_frame(1, 3), [0xA0, 0xA8, 0x82, 0x00, 0xA2, 0x01, 0x03]
        )
        self.assertEqual(
            ez.set_item_frame(2, -3),
            [0xA0, 0xA8, 0x82, 0x00, 0xA2, 0x02, 0xFD],
        )

    def test_set_volume_frame(self):
        # subopcode AC s raw 0–100 (živě ověřeno; clamp v builderu)
        self.assertEqual(
            ez.set_volume_frame(8), [0xA0, 0xA8, 0x82, 0x00, 0xAC, 8]
        )
        self.assertEqual(
            ez.set_volume_frame(101), [0xA0, 0xA8, 0x82, 0x00, 0xAC, 100]
        )
        self.assertEqual(
            ez.set_volume_frame(-5), [0xA0, 0xA8, 0x82, 0x00, 0xAC, 0]
        )

    def test_input_frames(self):
        self.assertEqual(ez.sam_request_frame(), [0x70, 0x00, 0x00])
        self.assertEqual(ez.active_source_frame((0x31, 0x00)), [0x82, 0x31, 0x00])
        self.assertEqual(ez.standby_frame(), [0x36])


class TestParsing(unittest.TestCase):
    def test_all_params(self):
        # živě odchycený A1: MOVIE / PRO / bass 0 / tre 0 / dimmer menu-0
        frame = [0xA0, 0xA8, 0x82, 0x00, 0xA1, 0x01, 0x01, 0x80, 0x80, 0x01]
        self.assertEqual(
            ez.parse_all_params(frame),
            {"eq": 1, "surr": 1, "bass": 0, "treble": 0, "dimmer": 1},
        )

    def test_all_params_bass_negative(self):
        frame = [0xA0, 0xA8, 0x82, 0x00, 0xA1, 0x00, 0x02, 0x7B, 0x85, 0x04]
        st = ez.parse_all_params(frame)
        self.assertEqual(st["bass"], -5)
        self.assertEqual(st["treble"], 5)
        self.assertEqual(st["surr"], 2)

    def test_all_params_too_short(self):
        self.assertIsNone(ez.parse_all_params([0xA0, 0xA8, 0x82, 0x00, 0xA1]))

    def test_item_value(self):
        frame = [0xA0, 0xA8, 0x82, 0x00, 0xA4, 0x03, 0x00]
        self.assertEqual(ez.parse_item_value(frame), (3, 0))

    def test_support_features(self):
        frame = [0xA0, 0xA8, 0x82, 0x00, 0x91, 0x9F, 0x06]
        self.assertEqual(ez.parse_support_features(frame), (0x9F, 6))

    def test_power_status(self):
        self.assertEqual(ez.parse_power_status([0x90, 0x00]), "on")
        self.assertEqual(ez.parse_power_status([0x90, 0x01]), "standby")
        self.assertEqual(ez.parse_power_status([0x90, 0x99]), "unknown")
        self.assertIsNone(ez.parse_power_status([0x8F]))

    def test_audio_status(self):
        # odpověď na 71 Give Audio Status = 7A Report Audio Status (živě
        # `50:7A:<mute<<7|vol>`); 0x7D je System Audio Mode Status!
        self.assertEqual(ez.parse_audio_status([0x7A, 0x25]), (False, 37))
        self.assertEqual(ez.parse_audio_status([0x7A, 0x80]), (True, 0))
        self.assertIsNone(ez.parse_audio_status([0x7D, 0x01]))

    def test_routing_change(self):
        self.assertEqual(
            ez.parse_routing_change([0x80, 0x30, 0x00, 0x31, 0x00]),
            (0x31, 0x00),
        )
        # krátký payload (ne 5 bajtů) -> None
        self.assertIsNone(ez.parse_routing_change([0x80, 0x31, 0x00]))

    def test_physical_address(self):
        self.assertEqual(ez.parse_physical_address([0x82, 0x31, 0x00]), (0x31, 0x00))
        self.assertEqual(ez.pa_to_str((0x31, 0x00)), "3.1.0.0")
        self.assertEqual(ez.pa_from_str("3.1.0.0"), (0x31, 0x00))
        self.assertEqual(ez.pa_from_str("1.0.0.0"), (0x10, 0x00))
        with self.assertRaises(ValueError):
            ez.pa_from_str("3.1.0")
        with self.assertRaises(ValueError):
            ez.pa_from_str("3.1.0.g")


class TestSignedDecode(unittest.TestCase):
    def test_offset_0x80(self):
        self.assertEqual(ez.decode_signed(0x80), 0)
        self.assertEqual(ez.decode_signed(0x82), 2)
        self.assertEqual(ez.decode_signed(0x7B), -5)
        self.assertEqual(ez.decode_signed(0x85), 5)

    def test_raw_two_complement(self):
        # A4 po A2 zápisu: raw hodnoty padnou mimo offset okno
        self.assertEqual(ez.decode_signed(0x03), 3)
        self.assertEqual(ez.decode_signed(0xFD), -3)
        self.assertEqual(ez.decode_signed(0x00), 0)
        self.assertEqual(ez.decode_signed(0x05), 5)
        self.assertEqual(ez.decode_signed(0xFB), -5)

    def test_offset_0x7b_a5(self):
        self.assertEqual(ez.decode_signed(0x7B, base=0x7B), 0)
        self.assertEqual(ez.decode_signed(0x7E, base=0x7B), 3)
        self.assertEqual(ez.decode_signed(0x76, base=0x7B), -5)


class TestTextParsing(unittest.TestCase):
    def test_gateway_readable(self):
        # formát gatewaye MQTT rx (Frame::to_string())
        self.assertEqual(
            ez.frame_from_text("50:90:00 => AudioSystem to TV: <Report Power Status>[On]"),
            (5, 0, [0x90, 0x00]),
        )

    def test_bare_hex_line(self):
        self.assertEqual(
            ez.frame_from_text("50:A0:A8:82:00:A1:01:01:80:80:01"),
            (5, 0, [0xA0, 0xA8, 0x82, 0x00, 0xA1, 1, 1, 0x80, 0x80, 1]),
        )

    def test_libcec_traffic_marker(self):
        self.assertEqual(
            ez.frame_from_text("TRAFFIC: [ 1234567] >> 0F:87:A8:82:00"),
            (0, 0xF, [0x87, 0xA8, 0x82, 0x00]),
        )
        self.assertEqual(
            ez.frame_from_text("<< 01:90:00"),
            (0, 1, [0x90, 0x00]),
        )

    def test_garbage(self):
        self.assertIsNone(ez.frame_from_text("nonsense"))
        self.assertIsNone(ez.frame_from_text(""))
        self.assertIsNone(ez.frame_from_text("50"))  # samotná hlavička


class TestEncoders(unittest.TestCase):
    def test_gateway_message(self):
        self.assertEqual(
            ez.encode_gateway_message(0, 5, [0xA0, 0xA8, 0x82, 0x00, 0xA0]),
            "0x0;0x5;0xA0,0xA8,0x82,0x00,0xA0",
        )
        self.assertEqual(
            ez.encode_gateway_message(14, 15, [0x82, 0x31, 0x00]),
            "0xE;0xF;0x82,0x31,0x00",
        )

    def test_pycec_message(self):
        self.assertEqual(
            ez.encode_pycec_message(0, 5, [0xA0, 0xA8, 0x82, 0x00, 0xA0]),
            "05:A0:A8:82:00:A0\r\n",
        )


class TestSafety(unittest.TestCase):
    def test_blocked_subopcodes(self):
        for sub in (0xA6, 0xB0, 0xB2):
            self.assertEqual(ez.blocked_ezplay_sub(ez.ezplay_frame(sub)), sub)
        self.assertIsNone(ez.blocked_ezplay_sub(ez.ezplay_frame(0xA0)))
        self.assertIsNone(ez.blocked_ezplay_sub([0x70, 0x00, 0x00]))

    def test_eq_option_order(self):
        # 0=MUSIC 1=MOVIE 2=NEWS 3=SPORT 4=NIGHT 5=GAME 6=AI (živě ověřeno)
        self.assertEqual(
            ez.EQ_OPTIONS,
            ("MUSIC", "MOVIE", "NEWS", "SPORT", "NIGHT", "GAME", "AI EQ"),
        )
        # item2 = binární on/off na EZPlay wire; tri-state mód dá jen RTVR
        self.assertEqual(ez.SURROUND_OPTIONS, ("OFF", "ON"))

    def test_encode_bass_treble(self):
        # +0x80 kódování (živě: IR bas −3 → wire 0x7D; +1 → 0x81)
        self.assertEqual(ez.encode_bass_treble(-5), 0x7B)
        self.assertEqual(ez.encode_bass_treble(-3), 0x7D)
        self.assertEqual(ez.encode_bass_treble(0), 0x80)
        self.assertEqual(ez.encode_bass_treble(1), 0x81)
        self.assertEqual(ez.encode_bass_treble(5), 0x85)


if __name__ == "__main__":
    unittest.main()
