"""Constants for aiobookoo."""

from enum import StrEnum
from typing import Final

SCALE_START_NAMES: Final = ["BOOKOO"]
SERVICE_UUID = "00000ffe-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID_WEIGHT = "0000ff11-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID_COMMAND = "0000ff12-0000-1000-8000-00805f9b34fb"
CMD_BYTE1_PRODUCT_NUMBER = 0x03  # Command Data BYTE1
CMD_BYTE2_TYPE = 0x0A  # Command Data BYTE2
WEIGHT_BYTE1 = 0x03
WEIGHT_BYTE2 = 0x0B


class UnitMass(StrEnum):
    """Unit of mass."""

    GRAMS = "grams"
    OUNCES = "ounces"
