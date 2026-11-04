"""Diagnostics contain health metadata only, never credentials or telemetry."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .azure import azure_settings
from .const import CONF_MODE, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, MODE_WEBHOOK


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    settings = azure_settings(entry)
    hub = entry.runtime_data
    return {
        "mode": settings.get(CONF_MODE, MODE_WEBHOOK),
        "scan_interval": settings.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        "vehicle_count": len(hub.vehicles),
        "cursor_present": bool(hub.azure_cursor),
        "sync_running": bool(
            hub.azure_sync and hub.azure_sync.task and not hub.azure_sync.task.done()
        ),
    }
