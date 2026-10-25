import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.applights.controller import challenge_from_info, fresh_advertisement
from custom_components.applights.diagnostics import async_get_config_entry_diagnostics
from custom_components.applights.light import PRESETS
from custom_components.applights.protocol import (
    SERVICE_UUID,
    login_packet,
    program_packet,
    transform,
)
from custom_components.applights.transport import (
    AppLightsError,
    AppLightsTransport,
    InvalidAuth,
    UnsupportedDevice,
)

ADDRESS = "10:20:30:40:50:61"
PASSWORD = "1234"
INFO = SimpleNamespace(
    address=ADDRESS,
    name="Star SpinnerC",
    service_uuids=[SERVICE_UUID],
    manufacturer_data={0xCAFE: bytes.fromhex("0000000061222222222222")},
    connectable=True,
)


class Peripheral:
    def __init__(self, *, rejected=False, protocol=b"BTM2-5\0", fail_write=False, challenge=0x22):
        self.is_connected = True
        self.rejected = rejected
        self.protocol = protocol
        self.fail_write = fail_write
        self.challenge = challenge
        self.writes = []
        self.notify = None

    async def read_gatt_char(self, _uuid):
        return self.protocol

    async def start_notify(self, _uuid, callback):
        self.notify = callback

    async def stop_notify(self, _uuid):
        self.notify = None

    async def disconnect(self):
        self.is_connected = False

    async def write_gatt_char(self, _uuid, data, response):
        assert response
        self.writes.append(bytes(data))
        if len(self.writes) == 1:
            self.notify(
                None,
                transform(
                    bytes([0x37, 0x5A if self.rejected else 0xA5]), ADDRESS, 3, self.challenge
                ),
            )
        elif self.fail_write:
            raise OSError("synthetic failure")


def transport_for(peripheral, **kwargs):
    async def connect():
        return peripheral, peripheral.challenge

    return AppLightsTransport(ADDRESS, PASSWORD, connect, **kwargs)


def test_independent_wire_vectors():
    assert login_packet(PASSWORD, ADDRESS, 3, 0x22).hex() == "30e6f5849393"
    assert program_packet(0xFF, ADDRESS, 3, 0x22).hex() == "248dcd12"
    assert program_packet(0x69, ADDRESS, 3, 0x22).hex() == "24d4cb12"
    assert len(PRESETS) == len(set(PRESETS.values())) == 61
    assert PRESETS["Red, Green, Blue & White / Starry Night"] == 0x9A
    assert PRESETS["Red, Blue & White / Sparkle"] == 0x94


def test_manufacturer_challenge_and_exact_model():
    assert challenge_from_info(INFO, ADDRESS) == 0x22
    other = SimpleNamespace(**vars(INFO))
    other.name = "Star Spinner"
    assert challenge_from_info(other, ADDRESS) is None
    other.name = "Star SpinnerC"
    other.manufacturer_data = {1: bytes(11)}
    assert challenge_from_info(other, ADDRESS) is None


async def test_authenticate_send_idle_cleanup():
    p = Peripheral()
    transport = transport_for(p, idle_hold_seconds=0.01)
    await transport.execute(0xFF)
    assert [x.hex() for x in p.writes] == ["30e6f5849393", "248dcd12"]
    assert p.is_connected and transport.diagnostics()["session_authenticated"]
    await asyncio.wait_for(transport._idle_task, 1)
    assert not p.is_connected and p.notify is None
    assert transport.diagnostics()["idle_disconnects"] == 1
    await transport.close()


async def test_consecutive_commands_share_login_and_reset_hold():
    p = Peripheral()
    transport = transport_for(p, idle_hold_seconds=0.02)
    await transport.execute(0x69)
    first_idle = transport._idle_task
    await transport.execute(0xFF)
    assert first_idle.cancelled()
    assert transport._idle_task is not first_idle
    assert [x.hex() for x in p.writes] == ["30e6f5849393", "24d4cb12", "248dcd12"]
    assert transport.diagnostics()["connection_reuses"] == 1
    assert transport.diagnostics()["connections_established"] == 1
    assert transport.diagnostics()["last_command_reused_connection"] is True
    await asyncio.wait_for(transport._idle_task, 1)
    assert not p.is_connected
    await transport.close()


@pytest.mark.parametrize("drop", [False, True])
async def test_expired_or_lost_connection_gets_fresh_challenge(drop):
    first, second = Peripheral(), Peripheral(challenge=0x44)
    connect = AsyncMock(side_effect=[(first, 0x22), (second, 0x44)])
    transport = AppLightsTransport(ADDRESS, PASSWORD, connect, idle_hold_seconds=0.01)
    await transport.execute(0x60)
    if drop:
        first.is_connected = False
    else:
        await asyncio.wait_for(transport._idle_task, 1)
    await transport.execute(0xFF)
    assert connect.await_count == 2
    assert second.writes == [
        login_packet(PASSWORD, ADDRESS, 3, 0x44),
        program_packet(0xFF, ADDRESS, 3, 0x44),
    ]
    assert transport.diagnostics()["connection_reuses"] == 0
    await transport.close()


async def test_failed_reused_write_disconnects_without_replay():
    p = Peripheral()
    transport = transport_for(p)
    await transport.execute(0x60)
    p.fail_write = True
    with pytest.raises(AppLightsError, match="program delivery"):
        await transport.execute(0xFF)
    assert len(p.writes) == 3  # One login, one successful command, one failed command.
    assert not p.is_connected and transport._idle_task is None
    assert transport.diagnostics()["commands_sent"] == 1
    await transport.close()


async def test_close_cancels_hold_and_rejects_new_commands():
    p = Peripheral()
    transport = transport_for(p)
    await transport.execute(0xFF)
    idle = transport._idle_task
    await transport.close()
    assert idle.cancelled() and not p.is_connected
    assert transport._idle_task is None
    with pytest.raises(AppLightsError, match="unloading"):
        await transport.execute(0x60)
    assert len(p.writes) == 2


async def test_cancellation_during_write_releases_session():
    p = Peripheral()
    transport = transport_for(p)
    await transport.execute(0x60)
    writing = asyncio.Event()

    async def stalled_write(*args, **kwargs):
        writing.set()
        await asyncio.Event().wait()

    p.write_gatt_char = stalled_write
    command = asyncio.create_task(transport.execute(0xFF))
    await writing.wait()
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    assert not p.is_connected and transport._idle_task is None
    await transport.close()


async def test_command_waits_for_idle_disconnect_then_reconnects():
    first, second = Peripheral(), Peripheral()
    disconnecting, release = asyncio.Event(), asyncio.Event()

    async def disconnect():
        disconnecting.set()
        await release.wait()
        first.is_connected = False

    first.disconnect = disconnect
    connect = AsyncMock(side_effect=[(first, 0x22), (second, 0x22)])
    transport = AppLightsTransport(ADDRESS, PASSWORD, connect, idle_hold_seconds=0)
    await transport.execute(0x60)
    await disconnecting.wait()
    command = asyncio.create_task(transport.execute(0xFF))
    await asyncio.sleep(0)
    assert not command.done() and connect.await_count == 1
    release.set()
    await command
    assert connect.await_count == 2
    assert len(second.writes) == 2
    await transport.close()


async def test_failed_idle_disconnect_keeps_client_for_cleanup():
    p = Peripheral()
    transport = transport_for(p, idle_hold_seconds=0)
    disconnect = p.disconnect
    p.disconnect = AsyncMock(side_effect=OSError("private wire details"))
    await transport.execute(0xFF)
    await transport._idle_task
    assert transport.connection_open and not transport.diagnostics()["session_authenticated"]
    assert transport.diagnostics()["disconnect_failures"] == 1
    p.disconnect = disconnect
    await transport.close()
    assert not p.is_connected and transport._client is None


async def test_verification_does_not_operate_light():
    p = Peripheral()
    await transport_for(p).execute()
    assert len(p.writes) == 1
    assert not p.is_connected


@pytest.mark.parametrize(
    "kwargs,error,count",
    [
        ({"rejected": True}, InvalidAuth, 1),
        ({"protocol": b"BTM2-4"}, UnsupportedDevice, 0),
        ({"fail_write": True}, AppLightsError, 2),
    ],
)
async def test_errors_stop_without_retry(kwargs, error, count):
    p = Peripheral(**kwargs)
    with pytest.raises(error):
        await transport_for(p).execute(0xFF)
    assert len(p.writes) == count
    assert not p.is_connected


async def test_fresh_advertisement_uses_packet_callback_and_cleans_up(hass, monkeypatch):
    callbacks = {}
    removed = []

    def register(hass, callback, address):
        callbacks["packet"] = callback
        return lambda: removed.append("packet")

    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_register_advertisement_callback", register
    )
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_register_callback",
        lambda *a, **k: lambda: removed.append("scan"),
    )
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_last_service_info", lambda *a, **k: INFO
    )

    async def sweep(*args):
        await asyncio.sleep(60)

    monkeypatch.setattr("homeassistant.components.bluetooth.async_request_active_scan", sweep)
    task = asyncio.create_task(fresh_advertisement(hass, ADDRESS))
    await asyncio.sleep(0)
    assert not task.done()
    packet = SimpleNamespace(**vars(INFO))
    packet.name = ADDRESS  # Before the manager's cross-scanner name merge.
    callbacks["packet"](packet)
    assert await task is packet
    assert removed == ["packet", "scan"]


@pytest.fixture
def transport(monkeypatch, mock_bluetooth):
    state = SimpleNamespace(peripherals=[], rejected=False, fail_write=False)

    def factory(hass, address, password):
        async def connect():
            p = Peripheral(rejected=state.rejected, fail_write=state.fail_write)
            state.peripherals.append(p)
            return p, 0x22

        return AppLightsTransport(address, password, connect)

    monkeypatch.setattr("custom_components.applights.create_transport", factory)
    monkeypatch.setattr("custom_components.applights.config_flow.create_transport", factory)
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_discovered_service_info", lambda *a, **k: [INFO]
    )
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_address_present", lambda *a, **k: True
    )
    return state


async def test_manual_setup_checks_password_without_output(hass, transport, monkeypatch):
    monkeypatch.setattr(
        "custom_components.applights.async_setup_entry", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_init("applights", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_PASSWORD: PASSWORD}
    assert len(transport.peripherals[0].writes) == 1
    assert not transport.peripherals[0].is_connected
    await hass.async_block_till_done()


async def test_discovery_waits_for_password(hass, transport):
    result = await hass.config_entries.flow.async_init(
        "applights", context={"source": "bluetooth"}, data=INFO
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "password"
    assert not transport.peripherals
    transport.rejected = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert not transport.peripherals[0].is_connected


async def test_invalid_password_no_connection(hass, transport):
    result = await hass.config_entries.flow.async_init("applights", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS, CONF_PASSWORD: "too-long-password"}
    )
    assert result["errors"] == {"base": "invalid_password"}
    assert not transport.peripherals


async def test_platform_commands_assumed_state_errors_and_unload(hass, transport):
    entry = MockConfigEntry(
        domain="applights",
        title="Star Spinner",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = "light.star_spinner"
    value = hass.states.get(entity_id)
    assert value.state == "unknown"
    assert value.attributes["assumed_state"] is True
    assert len(value.attributes["effect_list"]) == 61
    assert not transport.peripherals
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, "effect": "Green / Steady"}, blocking=True
    )
    value = hass.states.get(entity_id)
    assert value.state == "on" and value.attributes["effect"] == "Green / Steady"
    assert transport.peripherals[0].writes[-1].hex() == "24d4cb12"
    p = transport.peripherals[0]
    await hass.services.async_call("light", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "off"
    assert p.is_connected and len(p.writes) == 3
    p.fail_write = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("light", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "unknown"
    assert not p.is_connected
    diag = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    assert ADDRESS not in diag and PASSWORD not in diag
    client = entry.runtime_data
    await hass.services.async_call("light", "turn_off", {"entity_id": entity_id}, blocking=True)
    p = transport.peripherals[-1]
    assert p.is_connected
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert client.closed and not p.is_connected
