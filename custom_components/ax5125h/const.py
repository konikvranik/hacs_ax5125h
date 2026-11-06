"""Constants for the Hisense AX5125H soundbar integration."""

DOMAIN = "ax5125h"
MANUFACTURER = "Hisense"
MODEL = "AX5125H"
TITLE = "Soundbar AX5125H"

# --- transports -------------------------------------------------------------
TRANSPORT_ESPHOME_MQTT = "esphome_mqtt"
TRANSPORT_ESPHOME = "esphome"
TRANSPORT_HDMI_CEC = "hdmi_cec"       # HA hdmi_cec integration (local/remote per YAML)

TRANSPORTS = (
    TRANSPORT_ESPHOME_MQTT,
    TRANSPORT_ESPHOME,
    TRANSPORT_HDMI_CEC,
)

# --- config keys ------------------------------------------------------------
CONF_TRANSPORT = "transport"
CONF_PREFIX = "prefix"                # esphome_mqtt: MQTT topic prefix
CONF_SERVICE_BASE = "service_base"    # esphome: base of esphome.<base>_cec_send
CONF_POWER_ENTITY = "power_entity"    # hdmi_cec: entity carrying power state
CONF_TV_ADDR = "tv_addr"
CONF_SB_ADDR = "sb_addr"
CONF_GW_ADDR = "gw_addr"
CONF_HDMI_PA = "hdmi_pa"              # physical address of HDMI IN, "3.1.0.0"
CONF_HDMI_LA = "hdmi_la"              # logical address of HDMI IN source device (e.g. 1)
CONF_POLL_INTERVAL = "poll_interval"
CONF_BUS_LOG_SWITCH = "bus_log_switch"  # entity_id of "CEC Bus Log Enabled"

# --- defaults ---------------------------------------------------------------
DEFAULT_PREFIX = "cec_gateway"
DEFAULT_TV_ADDR = 0                   # TV (spoofovaný zdroj vendor příkazů)
DEFAULT_SB_ADDR = 5                   # AudioSystem = soundbar
DEFAULT_GW_ADDR = 14                  # 0xE = CEC gateway / switch role
DEFAULT_HDMI_PA = "3.1.0.0"
DEFAULT_HDMI_LA = 1                   # 1 = Recording 1 / Playback device behind soundbar
DEFAULT_POLL_INTERVAL = 30
MIN_POLL_INTERVAL = 10
