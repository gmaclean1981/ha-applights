"""On/off and the model-specific preset menu, with explicitly assumed state."""

import asyncio

from homeassistant.components import bluetooth
from homeassistant.components.light import ATTR_EFFECT, ColorMode, LightEntity, LightEntityFeature
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .const import DOMAIN, MODEL
from .protocol import STAR_SPINNER_EFFECT_OFFSETS, STAR_SPINNER_THEMES, star_spinner_program
from .transport import AppLightsError, InvalidAuth, PasswordChangeUncertain

PRESETS = {
    f"{theme} / {effect}": star_spinner_program(theme, effect)
    for theme in STAR_SPINNER_THEMES
    for effect in (["LightShow"] if theme == "LightShow" else STAR_SPINNER_EFFECT_OFFSETS)
}
PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AppLightsLight(entry)])


class AppLightsLight(LightEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_assumed_state = True
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(PRESETS)
    _attr_icon = "mdi:string-lights"
    _attr_extra_state_attributes = {"state_source": "last_successful_command", "protocol": "BTM2-5"}

    def __init__(self, entry):
        self._entry = entry
        self._address = entry.data[CONF_ADDRESS]
        self._transport = entry.runtime_data
        self._operation_lock = asyncio.Lock()
        self._attr_unique_id = f"{self._address}_light"
        self._attr_is_on = None
        self._attr_effect = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(CONNECTION_BLUETOOTH, self._address)},
            name="Star Spinner",
            manufacturer="AppLights",
            model=MODEL,
        )

    @property
    def available(self):
        return bluetooth.async_address_present(self.hass, self._address, connectable=True)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        @callback
        def discovered(*_):
            self.async_write_ha_state()

        @callback
        def unavailable(_):
            # A power cycle or another controller may have changed the state.
            self._attr_is_on = None
            self._attr_effect = None
            self.async_write_ha_state()

        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                discovered,
                {"address": self._address, "connectable": True},
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )
        self.async_on_remove(
            bluetooth.async_track_unavailable(
                self.hass,
                unavailable,
                self._address,
                connectable=True,
            )
        )

    async def _execute(self, program, is_on, effect=None):
        async with self._operation_lock:
            try:
                await self._transport.execute(program)
            except AppLightsError as exc:
                self._attr_is_on = None
                self._attr_effect = None
                self.async_write_ha_state()
                if isinstance(exc, InvalidAuth):
                    self._entry.async_start_reauth(self.hass)
                if isinstance(exc, PasswordChangeUncertain):
                    raise HomeAssistantError(
                        "Finish password verification in the AppLights integration settings"
                    ) from None
                raise HomeAssistantError(
                    "AppLights command failed; check range, password, and that AppLights is closed"
                ) from None
            self._attr_is_on = is_on
            self._attr_effect = effect
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None and effect not in PRESETS:
            raise ServiceValidationError("Choose an effect from the Star Spinner preset list")
        await self._execute(PRESETS[effect] if effect else 0x60, True, effect)

    async def async_turn_off(self, **kwargs):
        await self._execute(0xFF, False)
