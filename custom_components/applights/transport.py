"""Authenticated BLE sessions with a short idle hold and no command replay."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .protocol import (
    NOTIFY_UUID,
    TYPE_UUID,
    WRITE_UUID,
    login_packet,
    password_change_packet,
    program_packet,
    transform,
)

_LOGGER = logging.getLogger(__name__)
IDLE_HOLD_SECONDS = 30
REPLY_TIMEOUT_SECONDS = 8


class AppLightsError(Exception):
    """Safe error text, excluding credentials and BLE payloads."""


class InvalidAuth(AppLightsError):
    """The device explicitly rejected the existing password."""


class UnsupportedDevice(AppLightsError):
    """The model's protocol differs from the verified one."""


class PasswordChangeRejected(AppLightsError):
    """The device explicitly rejected the password change."""


class PasswordChangeNotApplied(AppLightsError):
    """The new password was rejected and the original still authenticates."""


class PasswordChangeUncertain(AppLightsError):
    """A write may have taken effect; verification is required before more commands."""


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
        self._reply = None
        self._pending_password = None
        self.closed = False

    @property
    def connection_open(self):
        return self._client is not None and self._client.is_connected

    @property
    def password_change_pending(self):
        return self._pending_password is not None

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
            "password_change_pending": self.password_change_pending,
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
        self._reply = None
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
            if self.password_change_pending:
                raise PasswordChangeUncertain("Finish password verification in AppLights settings")
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

    async def _request_reply(self, client, packet, opcode):
        accepted = asyncio.get_running_loop().create_future()
        self._reply = (opcode, accepted)
        try:
            async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
                await client.write_gatt_char(WRITE_UUID, packet, response=True)
                return await accepted
        finally:
            self._reply = None

    async def _authenticate(self, client, challenge, password=None):
        stage = "protocol identification"
        try:
            async with asyncio.timeout(25):
                identifier = await client.read_gatt_char(TYPE_UUID)
                if bytes(identifier).rstrip(b"\0") != b"BTM2-5":
                    raise UnsupportedDevice("Only Star SpinnerC / BTM2-5 is supported")
                stage = "password login"

                def notification(_characteristic, value):
                    decoded = transform(bytes(value), self._address, 3, challenge)
                    reply = self._reply
                    if (
                        reply
                        and len(decoded) == 2
                        and decoded[0] == reply[0]
                        and not reply[1].done()
                    ):
                        reply[1].set_result(decoded[1])

                await client.start_notify(NOTIFY_UUID, notification)
                self._notifying = True
                result = await self._request_reply(
                    client,
                    login_packet(
                        self._password if password is None else password,
                        self._address,
                        3,
                        challenge,
                    ),
                    0x37,
                )
                if result == 0x5A:
                    raise InvalidAuth("The light rejected the password")
                if result != 0xA5:
                    raise AppLightsError("Unexpected login response")
        except AppLightsError:
            raise
        except Exception as exc:
            raise AppLightsError(f"Bluetooth {stage} failed ({type(exc).__name__})") from None

    async def _fresh_login_locked(self, password):
        await self._disconnect_locked()
        client, challenge = await self._connect()
        self._client = client
        self._connections += 1
        if challenge is None:
            raise AppLightsError("No usable connection challenge")
        await self._authenticate(client, challenge, password)
        self._challenge = challenge
        return client, challenge

    async def _cleanup_password_locked(self):
        try:
            await self._disconnect_locked()
        except AppLightsError:
            _LOGGER.warning("Bluetooth cleanup failed during password management")

    async def verify_password(self, password):
        """Verify a replacement saved credential without changing the device."""
        login_packet(password, self._address, 3, 0)
        async with self._lock:
            if self.closed:
                raise AppLightsError("Integration is unloading")
            if self.password_change_pending:
                raise PasswordChangeUncertain("Finish password verification first")
            await self._cancel_idle_locked()
            try:
                await self._fresh_login_locked(password)
                await self._disconnect_locked()
            except AppLightsError:
                raise
            except Exception as exc:
                raise AppLightsError(
                    f"Password verification failed ({type(exc).__name__})"
                ) from None
            finally:
                await self._cleanup_password_locked()

    async def _resolve_password_locked(self):
        """Try the new credential, then the old only on explicit rejection. No writes."""
        replacement = self._pending_password
        if replacement is None:
            raise AppLightsError("No password change is pending")
        try:
            try:
                await self._fresh_login_locked(replacement)
                verified = replacement
            except InvalidAuth:
                await self._fresh_login_locked(self._password)
                verified = self._password
            await self._disconnect_locked()
        except Exception:
            raise PasswordChangeUncertain(
                "The password change needs verification; do not repeat the change"
            ) from None
        self._password = verified
        self._pending_password = None
        return verified

    async def resolve_password_change(self):
        """Retry verification only, including after an interrupted settings flow."""
        async with self._lock:
            if self.closed:
                raise AppLightsError("Integration is unloading")
            await self._cancel_idle_locked()
            try:
                return await self._resolve_password_locked()
            finally:
                await self._cleanup_password_locked()

    async def change_password(self, replacement):
        """Change once, then verify on a fresh connection; never replay the write."""
        password_change_packet(replacement, self._address, 3, 0)
        async with self._lock:
            if self.closed:
                raise AppLightsError("Integration is unloading")
            if self.password_change_pending:
                raise PasswordChangeUncertain("Finish password verification first")
            if replacement == self._password:
                raise ValueError("Choose a different password")
            await self._cancel_idle_locked()
            try:
                client, challenge = await self._fresh_login_locked(self._password)
                packet = password_change_packet(replacement, self._address, 3, challenge)
                # Set before attempting the write: even cancellation or timeout can
                # occur after the peripheral has committed its new credential.
                self._pending_password = replacement
                try:
                    result = await self._request_reply(client, packet, 0x58)
                except Exception:
                    result = None  # Resolve by fresh login, never by sending again.
                if result == 0x5A:
                    self._pending_password = None
                    raise PasswordChangeRejected("The light rejected the password change")
                verified = await self._resolve_password_locked()
                if verified != replacement:
                    raise PasswordChangeNotApplied(
                        "The original password still works; change not applied"
                    )
                _LOGGER.info("AppLights password change verified on a fresh connection")
            except AppLightsError:
                raise
            except Exception as exc:
                raise AppLightsError(f"Password change failed ({type(exc).__name__})") from None
            finally:
                await self._cleanup_password_locked()

    async def close(self):
        self.closed = True
        async with self._lock:
            await self._cancel_idle_locked()
            await self._disconnect_locked()
