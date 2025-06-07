"""Helper functions, taken and adapted from aioacia."""

import logging
from typing import cast, List  # Added List and Callable for other hints

from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

from .const import CHARACTERISTIC_UUID_WEIGHT, SCALE_START_NAMES
from .exceptions import BookooDeviceNotFound, BookooError, BookooUnknownDevice

_LOGGER = logging.getLogger(__name__)


async def find_bookoo_devices(
    timeout: float = 10.0, scanner: BleakScanner | None = None
) -> list[BLEDevice]:
    """Find BOOKOO devices by scanning and then filtering by name."""
    _LOGGER.debug("Attempting to find Bookoo devices with timeout: %s s", timeout)

    all_devices: list[BLEDevice]
    if scanner is None:
        # If no scanner is provided, create one for the duration of this function call.
        _LOGGER.debug("No existing scanner provided, creating a new BleakScanner.")
        async with BleakScanner() as new_scanner:
            all_devices = await scan(new_scanner, timeout)
    else:
        # Use the scanner provided by the caller.
        _LOGGER.debug("Using provided BleakScanner.")
        all_devices = await scan(scanner, timeout)

    if not all_devices:
        _LOGGER.debug("No BLE devices found by the scan function.")
        return []  # Return empty list if scan found nothing

    _LOGGER.debug(
        "Scan found %d BLE devices. Filtering for Bookoo scales by name prefixes: %s",
        len(all_devices),
        SCALE_START_NAMES,
    )
    bookoo_devices: list[BLEDevice] = []
    for device in all_devices:
        if device.name and any(
            device.name.startswith(name_prefix) for name_prefix in SCALE_START_NAMES
        ):
            _LOGGER.debug(
                "Found matching Bookoo device: Name='%s', Address='%s'",
                device.name,
                device.address,
            )
            bookoo_devices.append(device)
        else:
            _LOGGER.debug(
                "Device Name='%s' (Address='%s') did not match Bookoo prefixes.",
                device.name,
                device.address,
            )

    if not bookoo_devices:
        _LOGGER.debug("No devices found matching Bookoo name prefixes after filtering.")
        # Consider if BookooDeviceNotFound should be raised here if strict discovery is needed.
        # For now, returning an empty list indicates no specific Bookoo devices were found.

    return bookoo_devices


async def scan(scanner: BleakScanner, timeout: float) -> list[BLEDevice]:
    """Scan for BLE devices and return all discovered ones."""
    _LOGGER.debug("Scanning for BLE devices with timeout: %s", timeout)
    devices = await scanner.discover(timeout=timeout)
    for d in devices:
        # Log all found devices for debugging, filtering happens elsewhere or not at all in this func
        _LOGGER.debug(
            "Discovered device: Name=%s, Address=%s, Details=%s",
            d.name,
            d.address,
            d.details,
        )
    return cast(List[BLEDevice], devices)


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
