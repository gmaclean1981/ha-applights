"""Discover and verify the owner's existing password without changing the light."""

import logging
import re

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.helpers import selector

from .const import DOMAIN, MODEL
from .controller import challenge_from_info, create_transport
from .transport import AppLightsError, InvalidAuth, UnsupportedDevice

PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_LOGGER = logging.getLogger(__name__)


class AppLightsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _verify(self, password):
        if not 1 <= len(password.encode("utf-8")) <= 8:
            return "invalid_password"
        transport = create_transport(self.hass, self._address, password)
        try:
            await transport.execute()
        except InvalidAuth:
            return "invalid_auth"
        except UnsupportedDevice:
            return "not_supported"
        except (AppLightsError, OSError, TimeoutError) as exc:
            # AppLightsError messages are sanitized at the transport boundary.
            _LOGGER.warning(
                "AppLights setup failed: %s",
                str(exc) if isinstance(exc, AppLightsError) else type(exc).__name__,
            )
            return "cannot_connect"
        finally:
            await transport.close()
        return None

    async def _save(self, password):
        if self.source == "reauth":
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={CONF_PASSWORD: password}
            )
        return self.async_create_entry(
            title="Star Spinner", data={CONF_ADDRESS: self._address, CONF_PASSWORD: password}
        )

    async def async_step_bluetooth(self, discovery_info):
        self._address = discovery_info.address.upper()
        if challenge_from_info(discovery_info, self._address) is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": MODEL}
        return await self.async_step_password()

    async def async_step_password(self, user_input=None):
        errors = {}
        if user_input is not None:
            error = await self._verify(user_input[CONF_PASSWORD])
            if error is None:
                return await self._save(user_input[CONF_PASSWORD])
            errors["base"] = error
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD}),
            errors=errors,
            description_placeholders={"name": MODEL},
        )

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._address = str(user_input[CONF_ADDRESS]).strip().upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", self._address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(self._address)
                self._abort_if_unique_id_configured()
                error = await self._verify(user_input[CONF_PASSWORD])
                if error is None:
                    return await self._save(user_input[CONF_PASSWORD])
                errors["base"] = error
        options = [
            selector.SelectOptionDict(value=info.address.upper(), label=info.name)
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=True)
            if challenge_from_info(info, info.address.upper()) is not None
            and info.address.upper() not in self._async_current_ids()
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_PASSWORD): PASSWORD,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        self._address = entry_data[CONF_ADDRESS]
        return await self.async_step_password()
