"""Helper functions, taken from pybookoo."""

import logging

from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

from .const import (
    CHARACTERISTIC_UUID_WEIGHT,
    CMD_BYTE1_PRODUCT_NUMBER,
    CMD_BYTE2_TYPE,
    SCALE_START_NAMES,
)
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


def encode(msg_type: int, payload: bytearray | list[int]) -> bytearray:
    """Encode a message to the scale."""
    byte_msg = bytearray(5 + len(payload))

    byte_msg[0] = CMD_BYTE1_PRODUCT_NUMBER
    byte_msg[1] = CMD_BYTE2_TYPE
    byte_msg[2] = msg_type
    cksum1 = 0
    cksum2 = 0

    for i, p_byte in enumerate(payload):
        val = p_byte & 0xFF
        byte_msg[3 + i] = val
        if i % 2 == 0:
            cksum1 += val
        else:
            cksum2 += val

    byte_msg[len(payload) + 3] = cksum1 & 0xFF
    byte_msg[len(payload) + 4] = cksum2 & 0xFF

    return byte_msg
