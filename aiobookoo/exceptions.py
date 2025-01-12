"""Exceptions for aiobookoo."""

from bleak.exc import BleakDeviceNotFoundError, BleakError


class BookooScaleException(Exception):
    """Base class for exceptions in this module."""


class BookooDeviceNotFound(BleakDeviceNotFoundError):
    """Exception when no device is found."""


class BookooError(BleakError):
    """Exception for general bleak errors."""


class BookooUnknownDevice(Exception):
    """Exception for unknown devices."""


class BookooMessageError(Exception):
    """Exception for message errors."""

    def __init__(self, bytes_recvd: bytearray, message: str) -> None:
        super().__init__()
        self.message = message
        self.bytes_recvd = bytes_recvd


class BookooMessageTooShort(BookooMessageError):
    """Exception for messages that are too short."""

    def __init__(self, bytes_recvd: bytearray) -> None:
        super().__init__(bytes_recvd, "Message too short")


class BookooMessageTooLong(BookooMessageError):
    """Exception for messages that are too long."""

    def __init__(self, bytes_recvd: bytearray) -> None:
        super().__init__(bytes_recvd, "Message too long")
