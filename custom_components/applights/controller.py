"""Route through HA's Bluetooth manager, including active ESPHome proxies."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import callback

from .const import MODEL
from .protocol import SERVICE_UUID, advertisement_marker
from .transport import AppLightsError, AppLightsTransport


def challenge_from_info(info, address):
    if (
        info.address.upper() != address
        or info.name != MODEL
        or SERVICE_UUID not in info.service_uuids
    ):
        return None
    return challenge_from_manufacturer_data(info.manufacturer_data, address)


def challenge_from_manufacturer_data(manufacturer_data, address):
    marker = advertisement_marker(address)
    candidates = [
        data[5 + ((data[4] ^ data[5]) % 6)]
        for data in manufacturer_data.values()
        if len(data) == 11 and data[4] == marker
    ]
    return candidates[0] if len(candidates) == 1 else None


async def fresh_advertisement(hass, address):
    """Wait for a new packet, including unchanged data; never reuse an old challenge."""
    future = hass.loop.create_future()
    packets = 0

    @callback
    def received(info):
        nonlocal packets
        packets += 1
        # Raw callbacks run before HA merges its cross-scanner name cache.
        identity = bluetooth.async_last_service_info(hass, address, connectable=True) or info
        if (
            info.connectable
            and identity.name == MODEL
            and SERVICE_UUID in identity.service_uuids
            and challenge_from_manufacturer_data(info.manufacturer_data, address) is not None
            and not future.done()
        ):
            future.set_result(info)

    remove_raw = bluetooth.async_register_advertisement_callback(hass, received, address)
    remove_scan = None
    sweep = None
    try:
        # Requests active scan windows while using HA's shared scanner.
        remove_scan = bluetooth.async_register_callback(
            hass,
            lambda *_: None,
            {"address": address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        sweep = hass.async_create_task(bluetooth.async_request_active_scan(hass, 25))
        try:
            return await asyncio.wait_for(future, 25)
        except TimeoutError:
            raise AppLightsError(
                f"No usable fresh advertisement received ({packets} packets observed)"
            ) from None
    finally:
        remove_raw()
        if remove_scan is not None:
            remove_scan()
        if sweep is not None:
            sweep.cancel()
            with suppress(asyncio.CancelledError):
                await sweep


def create_transport(hass, address, password):
    async def connect():
        info = await fresh_advertisement(hass, address)
        device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if device is None:
            raise AppLightsError("No connectable Bluetooth adapter can reach the light")
        async with asyncio.timeout(35):
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                MODEL,
                max_attempts=1,
                ble_device_callback=lambda: (
                    bluetooth.async_ble_device_from_address(hass, address, connectable=True)
                    or device
                ),
            )
        return client, challenge_from_manufacturer_data(info.manufacturer_data, address)

    return AppLightsTransport(address, password, connect)
