"""Message decoding functions, taken from pybookoo."""

from dataclasses import dataclass
import logging

from .const import WEIGHT_BYTE1, WEIGHT_BYTE2
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
        self.unit: bytes = payload[5]
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


def decode(byte_msg: bytearray):
    """Return a tuple - first element is the message, or None.

    The second element is the remaining bytes of the message.

    """

    if len(byte_msg) < 20:
        raise BookooMessageTooShort(byte_msg)

    if len(byte_msg) > 20:
        raise BookooMessageTooLong(byte_msg)

    if byte_msg[0] == WEIGHT_BYTE1 and byte_msg[1] == WEIGHT_BYTE2:
        # _LOGGER.debug("Found valid weight Message")
        return (BookooMessage(byte_msg), bytearray())

    _LOGGER.debug("Full message: %s", byte_msg)
    return (None, byte_msg)
