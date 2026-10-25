"""Authenticated BLE sessions with a short idle hold and no command replay."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .protocol import NOTIFY_UUID, TYPE_UUID, WRITE_UUID, login_packet, program_packet, transform

_LOGGER = logging.getLogger(__name__)
IDLE_HOLD_SECONDS = 30


class AppLightsError(Exception):
    """Safe error text, excluding credentials and BLE payloads."""


class InvalidAuth(AppLightsError):
    """The device explicitly rejected the existing password."""


class UnsupportedDevice(AppLightsError):
    """The model's protocol differs from the verified one."""


class AppLightsTransport:
    """Reuse an authenticated connection briefly, releasing it after inactivity."""

    def __init__(self, address, password, connect, *, idle_hold_seconds=IDLE_HOLD_SECONDS):
        # Validate without retaining an encoded packet/challenge between sessions.
        login_packet(password, address, 3, 0)
        self._address = address
        self._password = password
        self._connect = connect
        self._lock = asyncio.Lock()
        self._client = None
        self._challenge = None
        self._notifying = False
        self._idle_task = None
        self._idle_hold_seconds = idle_hold_seconds
        self._connections = 0
        self._reuses = 0
        self._commands = 0
        self._idle_disconnects = 0
        self._close_failures = 0
        self._last_command_ms = None
        self._last_command_reused = None
        self.closed = False

    @property
    def connection_open(self):
        return self._client is not None and self._client.is_connected

    def diagnostics(self):
        """Session counters only; never return credentials or wire data."""
        return {
            "connection_open": self.connection_open,
            "session_authenticated": self.connection_open and self._challenge is not None,
            "idle_hold_seconds": self._idle_hold_seconds,
            "connections_established": self._connections,
            "connection_reuses": self._reuses,
            "commands_sent": self._commands,
            "idle_disconnects": self._idle_disconnects,
            "disconnect_failures": self._close_failures,
            "last_command_ms": self._last_command_ms,
            "last_command_reused_connection": self._last_command_reused,
        }

    async def _cancel_idle_locked(self):
        task, self._idle_task = self._idle_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _disconnect_locked(self):
        # Discard authentication even when disconnect fails. Retain that client
        # until cleanup succeeds so it cannot be abandoned for a second session.
        self._challenge = None
        client = self._client
        if client is None:
            return
        if self._notifying and client.is_connected:
            with suppress(Exception):
                await asyncio.wait_for(client.stop_notify(NOTIFY_UUID), 5)
        self._notifying = False
        if client.is_connected:
            try:
                await asyncio.wait_for(client.disconnect(), 10)
                if client.is_connected:
                    raise RuntimeError("Connection remains open")
            except Exception:
                self._close_failures += 1
                raise AppLightsError("Could not close the Bluetooth connection") from None
        self._client = None

    async def _expire_idle(self):
        try:
            await asyncio.sleep(self._idle_hold_seconds)
            async with self._lock:
                await self._disconnect_locked()
                self._idle_disconnects += 1
        except AppLightsError:
            _LOGGER.warning(
                "Could not release the idle Bluetooth connection; cleanup will be retried on the next command or unload"
            )
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    async def execute(self, program: int | None = None) -> None:
        if program is not None and program not in (0x60, 0xFF) and not 0x61 <= program <= 0x9D:
            raise ValueError("Unsupported Star SpinnerC program")
        async with self._lock:
            if self.closed:
                raise AppLightsError("Integration is unloading")
            await self._cancel_idle_locked()
            started = asyncio.get_running_loop().time()
            reused = self.connection_open and self._challenge is not None
            stage = "discovery/connection"
            try:
                if reused:
                    client, challenge = self._client, self._challenge
                    self._reuses += 1
                else:
                    await self._disconnect_locked()
                    client, challenge = await self._connect()
                    self._client = client
                    self._connections += 1
                    if challenge is None:
                        raise AppLightsError("No usable connection challenge")
                    await self._authenticate(client, challenge)
                    self._challenge = challenge
                if program is not None:
                    stage = "program delivery"
                    # A response confirms delivery, not independently read-back
                    # state. Never retry an ambiguous write, even on a held link.
                    async with asyncio.timeout(10):
                        await client.write_gatt_char(
                            WRITE_UUID,
                            program_packet(program, self._address, 3, challenge),
                            response=True,
                        )
                    self._commands += 1
                    self._last_command_ms = round(
                        (asyncio.get_running_loop().time() - started) * 1000, 1
                    )
                    self._last_command_reused = reused
                    self._idle_task = asyncio.create_task(
                        self._expire_idle(), name="applights_idle_disconnect"
                    )
                else:
                    # Setup only verifies a password and never holds a proxy slot.
                    await self._disconnect_locked()
            except BaseException as exc:
                try:
                    await self._disconnect_locked()
                except AppLightsError:
                    _LOGGER.warning("Bluetooth cleanup failed after an unsuccessful operation")
                if isinstance(exc, AppLightsError | asyncio.CancelledError):
                    raise
                if isinstance(exc, Exception):
                    raise AppLightsError(
                        f"Bluetooth {stage} failed ({type(exc).__name__})"
                    ) from None
                raise

    async def _authenticate(self, client, challenge):
        stage = "protocol identification"
        try:
            async with asyncio.timeout(25):
                identifier = await client.read_gatt_char(TYPE_UUID)
                if bytes(identifier).rstrip(b"\0") != b"BTM2-5":
                    raise UnsupportedDevice("Only Star SpinnerC / BTM2-5 is supported")
                accepted = asyncio.get_running_loop().create_future()
                stage = "password login"

                def notification(_characteristic, value):
                    decoded = transform(bytes(value), self._address, 3, challenge)
                    if len(decoded) == 2 and decoded[0] == 0x37 and not accepted.done():
                        accepted.set_result(decoded[1])

                await client.start_notify(NOTIFY_UUID, notification)
                self._notifying = True
                await client.write_gatt_char(
                    WRITE_UUID,
                    login_packet(self._password, self._address, 3, challenge),
                    response=True,
                )
                result = await asyncio.wait_for(accepted, 8)
                if result == 0x5A:
                    raise InvalidAuth("The light rejected the password")
                if result != 0xA5:
                    raise AppLightsError("Unexpected login response")
        except AppLightsError:
            raise
        except Exception as exc:
            raise AppLightsError(f"Bluetooth {stage} failed ({type(exc).__name__})") from None

    async def close(self):
        self.closed = True
        async with self._lock:
            await self._cancel_idle_locked()
            await self._disconnect_locked()
