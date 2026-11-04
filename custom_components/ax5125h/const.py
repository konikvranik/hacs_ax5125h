"""Constants for the Hisense AX5125H soundbar integration."""

DOMAIN = "ax5125h"
MANUFACTURER = "Hisense"
MODEL = "AX5125H"
TITLE = "Soundbar AX5125H"

# --- transports -------------------------------------------------------------
TRANSPORT_ESPHOME_MQTT = "esphome_mqtt"
TRANSPORT_ESPHOME = "esphome"
TRANSPORT_PYCEC_TCP = "pycec_tcp"
TRANSPORT_LIBCEC = "libcec"

TRANSPORTS = (
    TRANSPORT_ESPHOME_MQTT,
    TRANSPORT_ESPHOME,
    TRANSPORT_PYCEC_TCP,
    TRANSPORT_LIBCEC,
)

# --- config keys ------------------------------------------------------------
CONF_TRANSPORT = "transport"
CONF_PREFIX = "prefix"                # esphome_mqtt: MQTT topic prefix
CONF_HOST = "host"                    # pycec_tcp
CONF_PORT = "port"                    # pycec_tcp
CONF_SERVICE_BASE = "service_base"    # esphome: base of esphome.<base>_cec_send
CONF_ADAPTER = "adapter"              # libcec: COM port (empty = first found)
CONF_TV_ADDR = "tv_addr"
CONF_SB_ADDR = "sb_addr"
CONF_GW_ADDR = "gw_addr"
CONF_HDMI_PA = "hdmi_pa"              # physical address of HDMI IN, "3.1.0.0"
CONF_POLL_INTERVAL = "poll_interval"
CONF_BUS_LOG_SWITCH = "bus_log_switch"  # entity_id of "CEC Bus Log Enabled"

# --- defaults ---------------------------------------------------------------
DEFAULT_PREFIX = "cec_gateway"
DEFAULT_PORT = 9526                   # pyCEC TCP bridge
DEFAULT_TV_ADDR = 0                   # TV (spoofovaný zdroj vendor příkazů)
DEFAULT_SB_ADDR = 5                   # AudioSystem = soundbar
DEFAULT_GW_ADDR = 14                  # 0xE = CEC gateway / switch role
DEFAULT_HDMI_PA = "3.1.0.0"
DEFAULT_POLL_INTERVAL = 30
MIN_POLL_INTERVAL = 10
