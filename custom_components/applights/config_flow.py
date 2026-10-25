"""Local onboarding, optional password changes, and credential recovery."""

import re

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN, MODEL
from .controller import challenge_from_info, create_transport
from .transport import (
    AppLightsError,
    InvalidAuth,
    PasswordChangeNotApplied,
    PasswordChangeRejected,
    PasswordChangeUncertain,
    UnsupportedDevice,
)

DEFAULT_PASSWORD = "000000"
USE_DEFAULT = "use_factory_password"
CHANGE_PASSWORD = "change_password"
NEW_PASSWORD = "new_password"
CONFIRM_PASSWORD = "confirm_password"
PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def valid_password(value):
    try:
        return isinstance(value, str) and 1 <= len(value.encode("utf-8")) <= 8
    except UnicodeError:
        return False


def credentials_schema(*, allow_change=False):
    fields = {
        vol.Optional(USE_DEFAULT, default=False): selector.BooleanSelector(),
        vol.Optional(CONF_PASSWORD): PASSWORD,
    }
    if allow_change:
        fields[vol.Optional(CHANGE_PASSWORD, default=False)] = selector.BooleanSelector()
    return fields


def credential_from_input(user_input):
    password = user_input.get(CONF_PASSWORD, "")
    if user_input.get(USE_DEFAULT, False):
        if password:
            return None, "choose_one_password"
        return DEFAULT_PASSWORD, None
    return (password, None) if valid_password(password) else (None, "invalid_password")


def error_key(exc):
    if isinstance(exc, InvalidAuth):
        return "invalid_auth"
    if isinstance(exc, UnsupportedDevice):
        return "not_supported"
    if isinstance(exc, PasswordChangeRejected):
        return "change_rejected"
    if isinstance(exc, PasswordChangeNotApplied):
        return "change_not_applied"
    return "cannot_connect"


class PasswordChangeFlow:
    """Shared forms; secrets are never included in errors or placeholders."""

    _current_password = None
    _change_transport = None
    _change_step_id = "new_password"

    async def _change_form(self, step_id, user_input=None):
        self._change_step_id = step_id
        transport = self._get_change_transport()
        if transport.password_change_pending:
            return await self.async_step_recover_password()
        errors = {}
        if user_input is not None:
            replacement = user_input.get(NEW_PASSWORD, "")
            if not valid_password(replacement):
                errors[NEW_PASSWORD] = "invalid_password"
            elif replacement != user_input.get(CONFIRM_PASSWORD):
                errors[CONFIRM_PASSWORD] = "password_mismatch"
            elif replacement == self._current_password:
                errors[NEW_PASSWORD] = "same_password"
            else:
                try:
                    await transport.change_password(replacement)
                except PasswordChangeUncertain:
                    return await self.async_step_recover_password()
                except (AppLightsError, OSError, TimeoutError) as exc:
                    errors["base"] = error_key(exc)
                else:
                    return await self._save(replacement)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(NEW_PASSWORD): PASSWORD,
                    vol.Required(CONFIRM_PASSWORD): PASSWORD,
                }
            ),
            errors=errors,
        )

    async def async_step_recover_password(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                verified = await self._get_change_transport().resolve_password_change()
            except AppLightsError, OSError, TimeoutError:
                errors["base"] = "verification_pending"
            else:
                if verified != self._current_password:
                    return await self._save(verified)
                return self.async_show_form(
                    step_id=self._change_step_id,
                    data_schema=vol.Schema(
                        {
                            vol.Required(NEW_PASSWORD): PASSWORD,
                            vol.Required(CONFIRM_PASSWORD): PASSWORD,
                        }
                    ),
                    errors={"base": "change_not_applied"},
                )
        return self.async_show_form(
            step_id="recover_password", data_schema=vol.Schema({}), errors=errors
        )


class AppLightsConfigFlow(PasswordChangeFlow, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AppLightsOptionsFlow()

    async def _verify(self, password):
        transport = create_transport(self.hass, self._address, password)
        try:
            await transport.execute()
        except (AppLightsError, OSError, TimeoutError) as exc:
            return error_key(exc)
        finally:
            try:
                await transport.close()
            except AppLightsError:
                pass  # execute already reports its verification/cleanup failure.
        return None

    def _get_change_transport(self):
        if self._change_transport is None:
            self._change_transport = create_transport(
                self.hass, self._address, self._current_password
            )
        return self._change_transport

    async def _save(self, password):
        if self.source == "reauth":
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={CONF_PASSWORD: password}
            )
        return self.async_create_entry(
            title="Star Spinner", data={CONF_ADDRESS: self._address, CONF_PASSWORD: password}
        )

    async def _accept_credentials(self, user_input):
        password, error = credential_from_input(user_input)
        if error is None:
            error = await self._verify(password)
        if error:
            return None, error
        self._current_password = password
        if user_input.get(CHANGE_PASSWORD, False):
            return await self.async_step_new_password(), None
        return await self._save(password), None

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
            result, error = await self._accept_credentials(user_input)
            if error is None:
                return result
            errors["base"] = error
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(credentials_schema(allow_change=True)),
            errors=errors,
            description_placeholders={"name": MODEL},
        )

    async def async_step_new_password(self, user_input=None):
        return await self._change_form("new_password", user_input)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._address = str(user_input[CONF_ADDRESS]).strip().upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", self._address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(self._address)
                self._abort_if_unique_id_configured()
                result, error = await self._accept_credentials(user_input)
                if error is None:
                    return result
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
                    **credentials_schema(allow_change=True),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        self._address = entry_data[CONF_ADDRESS]
        return await self.async_step_password()


class AppLightsOptionsFlow(PasswordChangeFlow, OptionsFlow):
    """Manage credentials without creating a second BLE connection or device."""

    def _get_change_transport(self):
        return self.config_entry.runtime_data

    async def _save(self, password):
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_PASSWORD: password}
        )
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_init(self, user_input=None):
        if (
            not getattr(self.config_entry, "runtime_data", None)
            or self.config_entry.runtime_data.closed
        ):
            return self.async_abort(reason="entry_not_loaded")
        self._current_password = self.config_entry.data[CONF_PASSWORD]
        self._change_step_id = "change_password"
        if self._get_change_transport().password_change_pending:
            return await self.async_step_recover_password()
        return self.async_show_menu(
            step_id="init", menu_options=["change_password", "update_password"]
        )

    async def async_step_change_password(self, user_input=None):
        return await self._change_form("change_password", user_input)

    async def async_step_update_password(self, user_input=None):
        errors = {}
        if user_input is not None:
            password, error = credential_from_input(user_input)
            if error is None:
                try:
                    await self._get_change_transport().verify_password(password)
                except PasswordChangeUncertain:
                    return await self.async_step_recover_password()
                except (AppLightsError, OSError, TimeoutError) as exc:
                    error = error_key(exc)
                else:
                    return await self._save(password)
            errors["base"] = error
        return self.async_show_form(
            step_id="update_password",
            data_schema=vol.Schema(credentials_schema()),
            errors=errors,
        )
