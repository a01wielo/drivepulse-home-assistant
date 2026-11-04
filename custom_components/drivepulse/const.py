"""Constants for the DrivePulse integration."""

from __future__ import annotations

DOMAIN = "drivepulse"

CONF_LOCAL_ONLY = "local_only"
DEFAULT_LOCAL_ONLY = True

CONF_MODE = "mode"
MODE_WEBHOOK = "webhook"
MODE_AZURE = "azure"
CONF_API_URL = "api_url"
CONF_READ_TOKEN = "read_token"
CONF_VEHICLE = "vehicle"
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 30

EVENT_TRIP_START = "drivepulse_trip_start"
EVENT_TRIP_END = "drivepulse_trip_end"

# Messages the app sends (webhook schema 1).
MSG_TRIP_START = "trip_start"
MSG_TRIP_END = "trip_end"
MSG_LIVE = "live"
MSG_TEST = "test"
KNOWN_MESSAGES = {MSG_TRIP_START, MSG_TRIP_END, MSG_LIVE, MSG_TEST}

# More than this many vehicles on one webhook is not a garage, it is a forged
# or broken sender: further names are ignored instead of creating devices.
MAX_VEHICLES = 20
DEFAULT_VEHICLE_NAME = "DrivePulse"
