"""Password-management tests use a stateful fake light; no real radio access."""

import asyncio
import json

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.applights.config_flow import (
    CHANGE_PASSWORD,
    CONFIRM_PASSWORD,
    DEFAULT_PASSWORD,
    NEW_PASSWORD,
    USE_DEFAULT,
)
from custom_components.applights.protocol import password_change_packet, transform
from custom_components.applights.transport import (
    AppLightsTransport,
    InvalidAuth,
    PasswordChangeNotApplied,
    PasswordChangeRejected,
    PasswordChangeUncertain,
)

from .test_applights import ADDRESS, INFO, PASSWORD

NEW = "5678"


class FakeLight:
    def __init__(self, password=PASSWORD):
        self.password = password
        self.clients = []
        self.connect_calls = 0
        self.fail_connect = set()
        self.reject_change = False
        self.apply_change = True
        self.ack_change = True
        self.raise_after_write = False
        self.stall_change = False
        self.write_started = asyncio.Event()
        self.operations = []

    async def connect(self):
        self.connect_calls += 1
        if self.connect_calls in self.fail_connect:
            raise OSError("private connection detail")
        p = FakeConnection(self, challenge=self.connect_calls + 0x20)
        self.clients.append(p)
        return p, p.challenge

    def transport(self, password=None):
        return AppLightsTransport(
            ADDRESS, self.password if password is None else password, self.connect
        )


class FakeConnection:
    def __init__(self, light, challenge):
        self.light = light
        self.challenge = challenge
        self.is_connected = True
        self.notify = None
        self.authenticated = False

    async def read_gatt_char(self, _uuid):
        return b"BTM2-5"

    async def start_notify(self, _uuid, callback):
        self.notify = callback

    async def stop_notify(self, _uuid):
        self.notify = None

    async def disconnect(self):
        self.is_connected = False
        self.authenticated = False

    def reply(self, opcode, accepted):
        self.notify(
            None, transform(bytes([opcode, 0xA5 if accepted else 0x5A]), ADDRESS, 3, self.challenge)
        )

    async def write_gatt_char(self, _uuid, wire, response):
        assert self.is_connected and response
        payload = transform(bytes(wire), ADDRESS, 3, self.challenge)
        opcode = payload[0]
        self.light.operations.append(opcode)
        if opcode in (0x73, 0x85):
            raw = bytes(x ^ 0xA5 for x in payload[1:])
            assert raw[-1] == len(raw) - 1
            password = raw[:-1].decode("utf8")
        if opcode == 0x73:
            self.authenticated = password == self.light.password
            self.reply(0x37, self.authenticated)
        elif opcode == 0x85:
            assert self.authenticated
            if self.light.reject_change:
                self.reply(0x58, False)
                return
            if self.light.apply_change:
                self.light.password = password
            self.light.write_started.set()
            if self.light.stall_change:
                await asyncio.Event().wait()
            if self.light.raise_after_write:
                raise OSError("private payload " + password)
            if self.light.ack_change:
                self.reply(0x58, True)
        else:
            assert opcode == 0x67 and self.authenticated


def test_password_change_independent_vector_and_byte_limits():
    assert password_change_packet(NEW, ADDRESS, 3, 0x22).hex() == "c6e2f1809f93"
    password_change_packet("éééé", ADDRESS, 3, 0x22)
    for invalid in ("", "123456789", "ééééé"):
        with pytest.raises(ValueError):
            password_change_packet(invalid, ADDRESS, 3, 0x22)


async def test_change_verifies_on_new_connection_and_releases_held_slot():
    light = FakeLight()
    transport = light.transport()
    await transport.execute(0x60)
    held = light.clients[0]
    await transport.change_password(NEW)
    assert not held.is_connected
    assert light.password == NEW and not transport.password_change_pending
    assert light.operations == [0x73, 0x67, 0x73, 0x85, 0x73]
    assert all(not p.is_connected for p in light.clients)
    await transport.execute(0xFF)
    assert light.operations[-2:] == [0x73, 0x67]
    await transport.close()


@pytest.mark.parametrize("lost_ack", [True, False])
async def test_ambiguous_write_resolves_by_login_without_replay(lost_ack, monkeypatch):
    monkeypatch.setattr("custom_components.applights.transport.REPLY_TIMEOUT_SECONDS", 0.01)
    light = FakeLight()
    light.ack_change = not lost_ack
    light.raise_after_write = not lost_ack
    transport = light.transport()
    await transport.change_password(NEW)
    assert light.password == NEW
    assert light.operations.count(0x85) == 1
    assert light.operations == [0x73, 0x85, 0x73]
    assert not transport.password_change_pending
    await transport.close()


async def test_rejected_change_keeps_original_and_does_not_replay():
    light = FakeLight()
    light.reject_change = True
    transport = light.transport()
    with pytest.raises(PasswordChangeRejected):
        await transport.change_password(NEW)
    assert light.password == PASSWORD
    assert light.operations == [0x73, 0x85]
    assert not transport.password_change_pending
    await transport.close()


async def test_unapplied_change_requires_explicit_new_attempt(monkeypatch):
    monkeypatch.setattr("custom_components.applights.transport.REPLY_TIMEOUT_SECONDS", 0.01)
    light = FakeLight()
    light.apply_change = False
    light.ack_change = False
    transport = light.transport()
    with pytest.raises(PasswordChangeNotApplied):
        await transport.change_password(NEW)
    assert light.operations == [0x73, 0x85, 0x73, 0x73]
    assert light.password == PASSWORD and not transport.password_change_pending
    await transport.close()


async def test_failed_verification_blocks_writes_and_preserves_recovery():
    light = FakeLight()
    light.fail_connect = {2}
    transport = light.transport()
    with pytest.raises(PasswordChangeUncertain):
        await transport.change_password(NEW)
    assert light.password == NEW and transport.password_change_pending
    calls = light.connect_calls
    with pytest.raises(PasswordChangeUncertain):
        await transport.execute(0x60)
    with pytest.raises(PasswordChangeUncertain):
        await transport.change_password("9999")
    assert light.connect_calls == calls
    assert await transport.resolve_password_change() == NEW
    assert light.operations.count(0x85) == 1
    assert not transport.password_change_pending
    await transport.close()


async def test_cancellation_after_device_write_retains_verification_only_recovery():
    light = FakeLight()
    light.stall_change = True
    transport = light.transport()
    task = asyncio.create_task(transport.change_password(NEW))
    await light.write_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.password_change_pending and light.password == NEW
    assert all(not p.is_connected for p in light.clients)
    assert await transport.resolve_password_change() == NEW
    assert light.operations.count(0x85) == 1
    await transport.close()


async def test_wrong_current_password_cannot_change_device():
    light = FakeLight()
    transport = light.transport("wrong")
    with pytest.raises(InvalidAuth):
        await transport.change_password(NEW)
    assert light.operations == [0x73] and light.password == PASSWORD
    await transport.close()


async def test_recovery_diagnostics_and_errors_exclude_both_credentials(caplog):
    light = FakeLight(password="old-987")
    light.raise_after_write = True
    light.fail_connect = {2}
    transport = light.transport()
    with pytest.raises(PasswordChangeUncertain) as exc:
        await transport.change_password("new-654")
    visible = str(exc.value) + json.dumps(transport.diagnostics()) + caplog.text
    assert "old-987" not in visible and "new-654" not in visible
    assert ADDRESS not in visible and "private payload" not in visible
    await transport.close()


@pytest.fixture
def managed_light(monkeypatch, mock_bluetooth):
    light = FakeLight()

    def factory(hass, address, password):
        assert address == ADDRESS
        return light.transport(password)

    monkeypatch.setattr("custom_components.applights.create_transport", factory)
    monkeypatch.setattr("custom_components.applights.config_flow.create_transport", factory)
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_discovered_service_info", lambda *a, **k: [INFO]
    )
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_address_present", lambda *a, **k: True
    )
    return light


async def add_entry(hass, password=PASSWORD):
    entry = MockConfigEntry(
        domain="applights",
        title="Star Spinner",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_PASSWORD: password},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_factory_onboarding_without_change_sends_login_only(hass, managed_light):
    managed_light.password = DEFAULT_PASSWORD
    result = await hass.config_entries.flow.async_init("applights", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS, USE_DEFAULT: True}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == DEFAULT_PASSWORD
    assert managed_light.operations == [0x73]
    await hass.async_block_till_done()


async def test_factory_option_never_resets_a_custom_password(hass, managed_light):
    result = await hass.config_entries.flow.async_init(
        "applights", context={"source": "bluetooth"}, data=INFO
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {USE_DEFAULT: True})
    assert result["errors"] == {"base": "invalid_auth"}
    assert managed_light.password == PASSWORD and managed_light.operations == [0x73]


async def test_default_and_manual_password_conflict_has_no_ble_operation(hass, managed_light):
    result = await hass.config_entries.flow.async_init("applights", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS, USE_DEFAULT: True, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "choose_one_password"}
    assert not managed_light.operations


async def test_onboarding_optional_change_saves_only_verified_new_password(hass, managed_light):
    managed_light.password = DEFAULT_PASSWORD
    result = await hass.config_entries.flow.async_init(
        "applights", context={"source": "bluetooth"}, data=INFO
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {USE_DEFAULT: True, CHANGE_PASSWORD: True}
    )
    assert result["step_id"] == "new_password"
    assert managed_light.operations == [0x73]  # Nothing changed by opening the form.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {NEW_PASSWORD: NEW, CONFIRM_PASSWORD: NEW}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == NEW
    assert managed_light.password == NEW
    assert managed_light.operations == [0x73, 0x73, 0x85, 0x73]
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "new,confirm,error",
    [
        (NEW, "wrong", "password_mismatch"),
        ("ééééé", "ééééé", "invalid_password"),
        (PASSWORD, PASSWORD, "same_password"),
    ],
)
async def test_settings_validation_makes_no_connection(hass, managed_light, new, confirm, error):
    entry = await add_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "change_password"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {NEW_PASSWORD: new, CONFIRM_PASSWORD: confirm}
    )
    assert error in result["errors"].values()
    assert not managed_light.operations and entry.data[CONF_PASSWORD] == PASSWORD


async def test_later_change_updates_same_entry_and_reloads(hass, managed_light):
    entry = await add_entry(hass)
    unique_id = entry.unique_id
    before = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "change_password"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {NEW_PASSWORD: NEW, CONFIRM_PASSWORD: NEW}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PASSWORD] == NEW
    assert entry.unique_id == unique_id and before.closed
    assert hass.states.get("light.star_spinner") is not None
    assert managed_light.operations == [0x73, 0x85, 0x73]
    assert entry.options == {}


async def test_settings_recovery_after_cancel_sends_no_second_change(hass, managed_light):
    entry = await add_entry(hass)
    managed_light.fail_connect = {2}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "change_password"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {NEW_PASSWORD: NEW, CONFIRM_PASSWORD: NEW}
    )
    assert result["step_id"] == "recover_password"
    assert entry.data[CONF_PASSWORD] == PASSWORD
    assert NEW not in json.dumps({k: v for k, v in result.items() if k != "data_schema"})
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "recover_password"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PASSWORD] == NEW
    assert managed_light.operations.count(0x85) == 1


async def test_update_saved_password_after_restart_or_external_change(hass, managed_light):
    entry = await add_entry(hass)
    managed_light.password = DEFAULT_PASSWORD  # Physical reset or outside change.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "update_password"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {USE_DEFAULT: True}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PASSWORD] == DEFAULT_PASSWORD
    assert managed_light.operations == [0x73]
