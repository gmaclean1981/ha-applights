"""Local AppLights control; developed using an AI coding agent (OpenAI Codex)."""

from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD, EVENT_HOMEASSISTANT_STOP, Platform

from .controller import create_transport

PLATFORMS = [Platform.LIGHT]


async def async_setup_entry(hass, entry):
    transport = create_transport(hass, entry.data[CONF_ADDRESS], entry.data[CONF_PASSWORD])
    entry.runtime_data = transport
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await transport.close()
        raise

    async def stop(_event):
        await transport.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.close()
        return True
    return False
