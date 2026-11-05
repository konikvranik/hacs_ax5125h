"""Config flow testy v reálném HA harnessu (pytest-homeassistant-custom-component).

Reprodukuje a hlídá chybu 'Unknown error occurred' při přidávání integrace:
plný průchod flow všech tří transportů + setup entry.
"""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

DOMAIN = "ax5125h"

MQTT_DATA = {
    "transport": "esphome_mqtt",
    "prefix": "cec_gateway",
    "poll_interval": 30,
    "hdmi_pa": "3.1.0.0",
    "tv_addr": 0,
    "sb_addr": 5,
    "gw_addr": 14,
}


async def test_user_step_renders(hass: HomeAssistant, enable_custom_integrations) -> None:
    """První krok: výběr transportu (bez rozbalení formu — čistý render)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result.get("errors")


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_full_flow_mqtt(
    hass: HomeAssistant,
    enable_custom_integrations,
    mqtt_mock_entry,
    expected_lingering_timers: bool,
) -> None:
    """Celý MQTT flow: user -> esphome_mqtt (s cec_bus_log switchem!) -> common -> entry.

    Právě _bus_log_switches se switch stavy dřív padal (async_all vrací stringy).
    """
    await mqtt_mock_entry()
    hass.states.async_set("switch.cec_bus_log_enabled", "on")
    await hass.async_block_till_done()

    from tests.test_client import FakeTransport, default_script

    with patch(
        "custom_components.ax5125h.config_flow._async_validate",
        return_value=({"eq": 1, "power": "on"}, None),
    ), patch(
        "custom_components.ax5125h.build_transport",
        return_value=FakeTransport(default_script),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"transport": "esphome_mqtt"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "esphome_mqtt"
        assert not result.get("errors")
        # bus log switch je nabídnutý v formu
        labels = {str(k) for k in result["data_schema"].schema}
        assert "bus_log_switch" in labels

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"prefix": "cec_gateway", "bus_log_switch": "switch.cec_bus_log_enabled"}
        )
        assert result["step_id"] == "common"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"poll_interval": 30, "hdmi_pa": "3.1.0.0",
             "tv_addr": 0, "sb_addr": 5, "gw_addr": 14},
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    expected = {**MQTT_DATA, "bus_log_switch": "switch.cec_bus_log_enabled"}
    assert result["data"] == expected

    # entry se po CREATE_ENTRY auto-setupovala už uvnitř patch bloku
    # (fake transport) — ověříme stav
    assert result["result"].state is config_entries.ConfigEntryState.LOADED


async def test_full_flow_hdmi_cec(hass: HomeAssistant, enable_custom_integrations) -> None:
    """HDMI-CEC transport: validace služby, rx-less skip handshake."""
    async def fake_send(call):
        pass

    hass.services.async_register("hdmi_cec", "send_command", fake_send)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"transport": "hdmi_cec"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hdmi_cec"
    assert not result.get("errors")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"power_entity": "switch.hdmi_5"}
    )
    assert result["step_id"] == "common"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"poll_interval": 30, "hdmi_pa": "3.1.0.0",
         "tv_addr": 0, "sb_addr": 5, "gw_addr": 14},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    assert result["data"]["transport"] == "hdmi_cec"
    assert result["data"]["power_entity"] == "switch.hdmi_5"


async def test_hdmi_cec_missing_service(hass: HomeAssistant, enable_custom_integrations) -> None:
    """hdmi_cec bez služby -> chybová hláška, ne crash."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"transport": "hdmi_cec"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"power_entity": ""}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "hdmi_cec_missing"}


async def test_full_flow_esphome(hass: HomeAssistant, enable_custom_integrations) -> None:
    """ESPHome native transport: nabídne službu z registru."""
    async def fake_send(call):
        pass

    hass.services.async_register("esphome", "hdmi_cec_gateway_cec_send_src", fake_send)
    hass.services.async_register("esphome", "hdmi_cec_gateway_cec_send", fake_send)

    with patch(
        "custom_components.ax5125h.config_flow._async_validate",
        return_value=({"eq": 1, "power": "on"}, None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"transport": "esphome"}
        )
        assert result["step_id"] == "esphome"
        assert not result.get("errors")

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"service_base": "hdmi_cec_gateway"}
        )
        assert result["step_id"] == "common"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"poll_interval": 30, "hdmi_pa": "3.1.0.0",
             "tv_addr": 0, "sb_addr": 5, "gw_addr": 14},
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY, result


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_entry_setup_and_unload(
    hass: HomeAssistant,
    enable_custom_integrations,
    mqtt_mock_entry,
    expected_lingering_timers: bool,
) -> None:
    """Setup entry s fake transportem: coordinator refresh, entity vzniknou, unload čistý."""
    from tests.test_client import FakeTransport, default_script

    entry = MockConfigEntry(domain=DOMAIN, data=MQTT_DATA, entry_id="test_entry")
    entry.add_to_hass(hass)

    await mqtt_mock_entry()
    transport = FakeTransport(default_script)
    with patch(
        "custom_components.ax5125h.build_transport",
        return_value=transport,
    ) as mock_build:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        mock_build.assert_called_once()

    # coordinator data: eq=1 -> select "MOVIE", power on, bass 0
    # (entity_id se generuje z translation_key; nezavisle na naming)
    selects = hass.states.async_all("select")
    assert any(s.state == "MOVIE" for s in selects), [s.state for s in selects]
    numbers = hass.states.async_all("number")
    assert any(s.state in ("0", "0.0") for s in numbers), [s.state for s in numbers]
    switches = hass.states.async_all("switch")
    assert any(s.state == "on" for s in switches), [s.state for s in switches]

    # entity musí mít unique_id a být připojené k zařízení (bez unique_id
    # je HA 2025.x+ k zařízení nepřipojí — "no devices" bug)
    # + konkrétní ikony (ne generické placeholdery)
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    all_states = hass.states.async_all()
    missing_icons = [
        s.entity_id for s in all_states if not s.attributes.get("icon")
    ]
    assert not missing_icons, f"entity bez ikony: {missing_icons}"
    number_icons = {
        s.attributes.get("icon") for s in hass.states.async_all("number")
    }
    assert number_icons == {"mdi:music-clef-bass", "mdi:music-clef-treble"}

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    # 2026.10: async_get_device(identifiers=) deprecated → nové API
    device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "test_entry"), entry.entry_id
    )
    assert device is not None, "device registrace chybí"
    registered = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    # 3 senzory + 2 tlačítka + 2 přepínače + 4 selecty + 2 numbers = 13
    assert len(registered) == 13, [e.unique_id for e in registered]
    assert registered, "žádné registrované entity"
    for e in registered:
        assert e.unique_id, f"entity {e.entity_id} nemá unique_id"
        assert e.device_id == device.id, (
            f"entity {e.entity_id} není připojená k zařízení"
        )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rx_less_entry_power_from_entity(hass: HomeAssistant, enable_custom_integrations) -> None:
    """hdmi_cec transport entry: power se čte z konfigurované entity."""
    from tests.test_client import FakeTransport

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "transport": "hdmi_cec",
            "power_entity": "switch.hdmi_5",
            "poll_interval": 30, "hdmi_pa": "3.1.0.0",
            "tv_addr": 0, "sb_addr": 5, "gw_addr": 14,
        },
        entry_id="test_rxless",
    )
    entry.add_to_hass(hass)

    async def fake_send(call):
        pass

    hass.services.async_register("hdmi_cec", "send_command", fake_send)
    hass.states.async_set("switch.hdmi_5", "on")

    transport = FakeTransport(None)
    transport.supports_rx = False
    with patch("custom_components.ax5125h.build_transport", return_value=transport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    switches = hass.states.async_all("switch")
    assert any(s.state == "on" for s in switches), [s.state for s in switches]

    # A2 zápis: optimistic update bez rx (entity existuje a má EQ option)
    selects = hass.states.async_all("select")
    assert len(selects) >= 2


async def test_bus_log_autodetect_and_auto_enable(
    hass: HomeAssistant, enable_custom_integrations, mqtt_mock_entry
) -> None:
    """Flow bez vyplněného bus_log pole: switch se najde a zapne sám."""
    from tests.test_client import FakeTransport, default_script

    await mqtt_mock_entry()
    turn_on_calls = []

    async def fake_turn_on(call):
        turn_on_calls.append(dict(call.data))

    hass.services.async_register("switch", "turn_on", fake_turn_on)
    hass.states.async_set("switch.cec_bus_log_enabled", "off")

    with patch(
        "custom_components.ax5125h.config_flow._async_validate",
        return_value=({"eq": 1, "power": "on"}, None),
    ), patch(
        "custom_components.ax5125h.build_transport",
        return_value=FakeTransport(default_script),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"transport": "esphome_mqtt"}
        )
        # pole bus_log_switch NEvyplňujeme (má default ze suggestions)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"prefix": "cec_gateway"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"poll_interval": 30, "hdmi_pa": "3.1.0.0",
             "tv_addr": 0, "sb_addr": 5, "gw_addr": 14},
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    # switch byl OFF → _async_validate ho měla zapnout sama
    assert {"entity_id": "switch.cec_bus_log_enabled"} in turn_on_calls
