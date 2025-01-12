"""Helper functions, taken and adapted from aioacia."""

import logging

from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

from .const import CHARACTERISTIC_UUID_WEIGHT, SCALE_START_NAMES
from .exceptions import BookooDeviceNotFound, BookooError, BookooUnknownDevice

_LOGGER = logging.getLogger(__name__)


async def find_bookoo_devices(timeout=10, scanner: BleakScanner | None = None) -> list:
    """Find BOOKOO devices."""

    _LOGGER.debug("Looking for BOOKOO devices")
    if scanner is None:
        async with BleakScanner() as new_scanner:
            return await scan(new_scanner, timeout)
    else:
        return await scan(scanner, timeout)


async def scan(scanner: BleakScanner, timeout) -> list:
    """Scan for devices."""
    addresses = []

    devices = await scanner.discover(timeout=timeout)
    for d in devices:
        _LOGGER.debug("Found device with name: %s and address: %s", d.name, d.address)
        if d.name and any(d.name.startswith(name) for name in SCALE_START_NAMES):
            # print(d.name, d.address)
            addresses.append(d.address)

    return addresses


async def is_bookoo_scale(address_or_ble_device: str | BLEDevice) -> bool:
    """Check if the scale is a new style scale."""

    try:
        async with BleakClient(address_or_ble_device) as client:
            characteristics = [
                char.uuid for char in client.services.characteristics.values()
            ]
    except BleakDeviceNotFoundError as ex:
        raise BookooDeviceNotFound("Device not found") from ex
    except (BleakError, Exception) as ex:
        raise BookooError(ex) from ex

    if CHARACTERISTIC_UUID_WEIGHT in characteristics:
        return True

    raise BookooUnknownDevice
