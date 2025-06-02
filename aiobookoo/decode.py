"""Message decoding functions, taken from pybookoo."""

from dataclasses import dataclass
import logging

from .const import (
    WEIGHT_BYTE1, 
    WEIGHT_BYTE2, 
    CMD_BYTE1_PRODUCT_NUMBER, 
    CMD_BYTE2_MESSAGE_TYPE_AUTO_TIMER, 
    CMD_BYTE3_AUTO_TIMER_EVENT_START, 
    CMD_BYTE3_AUTO_TIMER_EVENT_STOP
)
from .exceptions import BookooMessageError, BookooMessageTooLong, BookooMessageTooShort

_LOGGER = logging.getLogger(__name__)


@dataclass
class BookooMessage:
    """Representation of the contents of a Datapacket from the weight Characteristic of a Bookoo Scale."""

    def __init__(self, payload: bytearray) -> None:
        """Initialize a Settings instance.

        :param payload: The payload containing the settings data.
        decode as described in https://github.com/BooKooCode/OpenSource/blob/main/bookoo_mini_scale/protocols.md
        """

        self.timer: float | None = (
            int.from_bytes(
                payload[3:5],
                byteorder="big",  # time in milliseconds
            )
            / 1000.0  # time in seconds
        )
        self.unit: int = payload[5]
        self.weight_symbol = -1 if payload[6] == 45 else 1 if payload[6] == 43 else 0
        self.weight: float | None = (
            int.from_bytes(payload[8:10], byteorder="big") / 100.0 * self.weight_symbol
        )  # Convert to grams

        self.flowSymbol = -1 if payload[10] == 45 else 1 if payload[10] == 43 else 0
        self.flow_rate = (
            int.from_bytes(payload[12:13], byteorder="big") / 100.0 * self.flowSymbol
        )  # Convert to ml
        self.battery = payload[13]  # battery level in percent
        self.standby_time = int.from_bytes(payload[14:15], byteorder="big")  # minutes
        self.buzzer_gear = payload[16]
        self.flow_rate_smoothing = payload[17]  # 0 = off, 1 = on

        # Verify checksum
        checksum = 0
        for byte in payload[:-1]:
            checksum ^= byte
        if checksum != payload[-1]:
            raise BookooMessageError(payload, "Checksum mismatch")

        # _LOGGER.debug(
        #     "Bookoo Message: unit=%s, weight=%s, time=%s, battery=%s, flowRate=%s, standbyTime=%s, buzzerGear=%s, flowRateSmoothing=%s",
        #     self.unit,
        #     self.weight,
        #     self.timer,
        #     self.battery,
        #     self.flow_rate,
        #     self.standby_time,
        #     self.buzzer_gear,
        #     self.flow_rate_smoothing,
        # )


def decode(byte_msg: bytearray) -> tuple[BookooMessage | dict | None, bytearray]:
    """Return a tuple - first element is the message, or None.

    The second element is the remaining bytes of the message.

    """

    # Check for Weight Characteristic Message (typically 20 bytes)
    if len(byte_msg) == 20 and byte_msg[0] == WEIGHT_BYTE1 and byte_msg[1] == WEIGHT_BYTE2:
        # Perform checksum for weight message before parsing
        checksum = 0
        for byte_val in byte_msg[:-1]:
            checksum ^= byte_val
        if checksum != byte_msg[-1]:
            _LOGGER.warning("Weight message checksum mismatch: %s", byte_msg.hex())
            # Decide if to raise BookooMessageError or return None
            # For now, let's be strict, as BookooMessage init will also check
            # raise BookooMessageError(byte_msg, "Checksum mismatch in decode function for weight")
            # Or, let BookooMessage handle it, but then we might pass bad data to it.
            # Let's return None for now if checksum fails here, to avoid BookooMessage init error.
            return (None, byte_msg) # Or raise error
        _LOGGER.debug("Found valid weight Message")
        return (BookooMessage(byte_msg), bytearray()) # BookooMessage also does checksum

    # Check for Command Characteristic Auto-Timer Messages (typically 20 bytes)
    # Assuming CMD_PRODUCT_NUMBER, CMD_TYPE_AUTO_TIMER, etc. are imported from .const
    # For example: from .const import CMD_PRODUCT_NUMBER, CMD_TYPE_AUTO_TIMER, CMD_EVENT_AUTO_TIMER_START, CMD_EVENT_AUTO_TIMER_STOP
    # These constants would be: 0x03, 0x0D, 0x01, 0x00 respectively.
    # Also assuming auto-timer messages are a fixed length, e.g., 20 bytes including checksum.
    # A more robust implementation would check message type first, then length for that type.
    if len(byte_msg) == 20 and byte_msg[0] == CMD_BYTE1_PRODUCT_NUMBER and byte_msg[1] == CMD_BYTE2_MESSAGE_TYPE_AUTO_TIMER:
        # Perform checksum for auto-timer message
        checksum = 0
        for byte_val in byte_msg[:-1]:
            checksum ^= byte_val
        if checksum != byte_msg[-1]:
            _LOGGER.warning("Auto-timer command message checksum mismatch: %s", byte_msg.hex())
            return (None, byte_msg) # Or raise error

        if byte_msg[2] == CMD_BYTE3_AUTO_TIMER_EVENT_START: # Auto-timer Start event
            _LOGGER.debug("Found auto-timer start command message")
            return ({'type': 'auto_timer', 'event': 'start'}, bytearray())
        elif byte_msg[2] == CMD_BYTE3_AUTO_TIMER_EVENT_STOP: # Auto-timer Stop event
            _LOGGER.debug("Found auto-timer stop command message")
            return ({'type': 'auto_timer', 'event': 'stop'}, bytearray())
        else:
            _LOGGER.debug("Known command prefix (0x030D) but unknown event: %s", byte_msg.hex())
    
    # Add checks for other command characteristic messages here if needed


    _LOGGER.debug("Full message: %s", byte_msg)
    return (None, byte_msg)
