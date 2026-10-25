"""Diagnostics deliberately exclude addresses, password and packet contents."""

from .const import MODEL, PROTOCOL


async def async_get_config_entry_diagnostics(hass, entry):
    return {
        "model": MODEL,
        "protocol_identifier": PROTOCOL,
        "state_reporting": "assumed; last successful command only",
        "preset_count": 61,
        **entry.runtime_data.diagnostics(),
        "transport": "Home Assistant Bluetooth, including active proxies",
    }
