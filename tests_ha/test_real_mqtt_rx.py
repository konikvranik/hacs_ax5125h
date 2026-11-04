"""Reálná MQTT rx/tx cesta v HA harnessu — žádný patch build_transport.

Tyhle vrstvy test_config_flow nepokrývá (FakeTransport) a právě tady
prosákly dvě živé regrese:
- 0.2.3: payload jako str → AttributeError 'str' has no 'decode'
  (HA mqtt komponent konvertuje payload; FakeTransport jdou jen list[int])
- 0.2.3: rx callback mimo event loop → cross-thread async_write_ha_state

Tady běží reálný MqttCecTransport + EzplayClient + coordinator + entity,
zprávy jdou přes reálný dispatch mqtt komponenty (async_fire_mqtt_message
→ _async_mqtt_on_message), tj. stejnou konverzi payloadu jako živý systém.
"""

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)
from unittest.mock import patch

DOMAIN = "ax5125h"

MQTT_DATA = {
    "transport": "esphome_mqtt",
    "prefix": "cec_gateway",
    "bus_log_switch": "switch.cec_bus_log_enabled",
    "poll_interval": 30,
    "hdmi_pa": "3.1.0.0",
    "tv_addr": 0,
    "sb_addr": 5,
    "gw_addr": 14,
}

#: reálné texty rámeců ze živé gatewayi (readable formát z cec_gateway/rx)
RX_A1 = (
    "50:A0:A8:82:00:A1:00:01:80:80:00"
    " => AudioSystem to TV: <Vendor Command With ID>[ID=a88200][.]"
)
RX_POWER_ON = "50:90:00 => AudioSystem to TV: <Report Power Status>[On]"
RX_SUPPORT = (
    "50:A0:A8:82:00:91:9F:06"
    " => AudioSystem to TV: <Vendor Command With ID>[ID=a88200][.]"
)


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_real_mqtt_rx_chain_updates_entities(
    hass: HomeAssistant,
    enable_custom_integrations,
    mqtt_mock_entry,
) -> None:
    """Setup s reálným transportem, rx přes reálný dispatch → entity se mění."""
    entry = MockConfigEntry(domain=DOMAIN, data=MQTT_DATA, entry_id="rx_entry")
    entry.add_to_hass(hass)
    mqtt_mock = await mqtt_mock_entry()

    # initial poll potlačíme (reálný handshake by čekal timeouty) —
    # transport/subscribe běží reálně
    with patch(
        "custom_components.ax5125h.client.EzplayClient.async_refresh",
        return_value={"power": "standby"},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is entry.state.LOADED

    # žádná EQ data zatím → select bez hodnoty
    eq_before = [s.state for s in hass.states.async_all("select")]
    assert "unknown" in eq_before or "unavailable" in eq_before, eq_before

    # --- rx: A1 (EQ=MUSIC, SURR=PRO, bas 0, výšky 0) přes reálný dispatch ---
    async_fire_mqtt_message(hass, "cec_gateway/rx", RX_A1)
    await hass.async_block_till_done()

    eq_states = [s.state for s in hass.states.async_all("select")]
    assert "MUSIC" in eq_states, eq_states

    # --- rx: Report Power Status [On] ---
    async_fire_mqtt_message(hass, "cec_gateway/rx", RX_POWER_ON)
    await hass.async_block_till_done()
    sensors = [s.state for s in hass.states.async_all("sensor")]
    assert "on" in sensors, sensors

    # --- tx: service volume_up → publish do cec_gateway/send ---
    eq_entity_id = next(
        s.entity_id
        for s in hass.states.async_all("select")
        if s.state == "MUSIC"
    )
    await hass.services.async_call(
        DOMAIN, "volume_up", {"entity_id": [eq_entity_id]}, blocking=True
    )
    await hass.async_block_till_done()
    sent = [
        call for call in mqtt_mock.async_publish.mock_calls
        if call.args and call.args[0] == "cec_gateway/send"
    ]
    assert sent, "volume_up neposlal rámec do cec_gateway/send"
    # UCP key press (0x44 vol+ / 0x41) + release (0x45) — bez spoofu
    payloads = [str(call.args[1]) for call in sent]
    assert any("0x44" in p for p in payloads), payloads
    assert any("0x45" in p for p in payloads), payloads


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_real_mqtt_rx_support_features_updates_diagnostic(
    hass: HomeAssistant,
    enable_custom_integrations,
    mqtt_mock_entry,
) -> None:
    """0x91 push aktualizuje diagnostický sensor (support features)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MQTT_DATA, entry_id="rx_entry2")
    entry.add_to_hass(hass)
    await mqtt_mock_entry()

    with patch(
        "custom_components.ax5125h.client.EzplayClient.async_refresh",
        return_value={"power": "on"},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    async_fire_mqtt_message(hass, "cec_gateway/rx", RX_SUPPORT)
    await hass.async_block_till_done()

    sensors = hass.states.async_all("sensor")
    support = [
        s for s in sensors if s.state == "0x9F"
    ]
    assert support, [(s.entity_id, s.state) for s in sensors]
    assert support[0].attributes.get("items") == 6
