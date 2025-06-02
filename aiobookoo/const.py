"""Constants for aiobookoo."""

from enum import StrEnum
from typing import Final

SCALE_START_NAMES: Final = ["BOOKOO"]
SERVICE_UUID = "00000ffe-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID_WEIGHT = "0000ff11-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID_COMMAND = "0000ff12-0000-1000-8000-00805f9b34fb"

# Constants for identifying the source of a notification callback
UPDATE_SOURCE_WEIGHT_CHAR = "weight_char_update"
UPDATE_SOURCE_COMMAND_CHAR = "command_char_notification"
CMD_BYTE1_PRODUCT_NUMBER = 0x03  # Command Data BYTE1
CMD_BYTE2_TYPE = 0x0A  # Command Data BYTE2 (General command type, e.g., for tare, timer control)
CMD_BYTE2_MESSAGE_TYPE_AUTO_TIMER = 0x0D # Specific message type for auto-timer events from scale
CMD_BYTE3_AUTO_TIMER_EVENT_START = 0x01   # Auto-timer event: start
CMD_BYTE3_AUTO_TIMER_EVENT_STOP = 0x00    # Auto-timer event: stop
WEIGHT_BYTE1 = 0x03
WEIGHT_BYTE2 = 0x0B


class UnitMass(StrEnum):
    """Unit of mass."""

    GRAMS = "grams"
    OUNCES = "ounces"
