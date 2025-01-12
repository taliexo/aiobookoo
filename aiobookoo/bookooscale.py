"""Client to interact with Bookoo scales."""

from __future__ import annotations  # noqa: I001

import asyncio
import logging
import time

from collections.abc import Awaitable, Callable

from dataclasses import dataclass

from bleak import BleakClient, BleakGATTCharacteristic, BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

from .const import (
    CHARACTERISTIC_UUID_WEIGHT,
    CHARACTERISTIC_UUID_COMMAND,
)
from .exceptions import (
    BookooDeviceNotFound,
    BookooError,
    BookooMessageError,
    BookooMessageTooLong,
    BookooMessageTooShort,
)
from .const import UnitMass
from .decode import BookooMessage, decode

_LOGGER = logging.getLogger(__name__)


@dataclass(kw_only=True)
class BookooDeviceState:
    """Data class for bookoo scale info data."""

    battery_level: int
    units: UnitMass
    buzzer_gear: int = 0
    auto_off_time: int = 0


class BookooScale:
    """Representation of a bookoo scale."""

    _weight_char_id = CHARACTERISTIC_UUID_WEIGHT
    _command_char_id = CHARACTERISTIC_UUID_COMMAND

    _msg_types = {
        "tare": bytearray([0x03, 0x0A, 0x01, 0x00, 0x00, 0x08]),
        "startTimer": bytearray([0x03, 0x0A, 0x04, 0x00, 0x00, 0x0A]),
        "stopTimer": bytearray([0x03, 0x0A, 0x05, 0x00, 0x00, 0x0D]),
        "resetTimer": bytearray([0x03, 0x0A, 0x06, 0x00, 0x00, 0x0C]),
        "tareAndStartTime": bytearray([0x03, 0x0A, 0x07, 0x00, 0x00, 0x00]),
    }

    def __init__(
        self,
        address_or_ble_device: str | BLEDevice,
        name: str | None = None,
        is_valid_scale: bool = True,
        notify_callback: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the scale."""

        self._is_valid_scale = is_valid_scale
        self._client: BleakClient | None = None

        self.address_or_ble_device = address_or_ble_device
        self.model = "Themis"
        self.name = name

        # tasks
        self.process_queue_task: asyncio.Task | None = None

        # connection diagnostics
        self.connected = False
        self._timestamp_last_command: float | None = None
        self.last_disconnect_time: float | None = None

        self._device_state: BookooDeviceState | None = None
        self._weight: float | None = None
        self._timer: float | None = None
        self._flow_rate: float | None = None

        # queue
        self._queue: asyncio.Queue = asyncio.Queue()
        self._add_to_queue_lock = asyncio.Lock()

        self._last_short_msg: bytearray | None = None

        self._notify_callback: Callable[[], None] | None = notify_callback

    @property
    def mac(self) -> str:
        """Return the mac address of the scale in upper case."""
        return (
            self.address_or_ble_device.upper()
            if isinstance(self.address_or_ble_device, str)
            else self.address_or_ble_device.address.upper()
        )

    @property
    def device_state(self) -> BookooDeviceState | None:
        """Return the device info of the scale."""
        return self._device_state

    @property
    def weight(self) -> float | None:
        """Return the weight of the scale."""
        return self._weight

    @property
    def timer(self) -> int:
        """Return the current timer value in seconds."""
        return self._timer

    @property
    def flow_rate(self) -> float | None:
        """Calculate the current flow rate."""

        return self._flow_rate

    def device_disconnected_handler(
        self,
        client: BleakClient | None = None,  # pylint: disable=unused-argument
        notify: bool = True,
    ) -> None:
        """Handle device disconnection."""

        _LOGGER.debug(
            "Scale with address %s disconnected through disconnect handler",
            self.mac,
        )

        self.connected = False
        self.last_disconnect_time = time.time()
        self.async_empty_queue_and_cancel_tasks()
        if notify and self._notify_callback:
            self._notify_callback()

    async def _write_msg(self, char_id: str, payload: bytearray) -> None:
        """Write to the device."""
        if self._client is None:
            raise BookooError("Client not initialized")
        try:
            await self._client.write_gatt_char(char_id, payload)
            self._timestamp_last_command = time.time()
        except BleakDeviceNotFoundError as ex:
            self.connected = False
            raise BookooDeviceNotFound("Device not found") from ex
        except BleakError as ex:
            self.connected = False
            raise BookooError("Error writing to device") from ex
        except TimeoutError as ex:
            self.connected = False
            raise BookooError("Timeout writing to device") from ex
        except Exception as ex:
            self.connected = False
            raise BookooError("Unknown error writing to device") from ex

    def async_empty_queue_and_cancel_tasks(self) -> None:
        """Empty the queue."""

        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

        if self.process_queue_task and not self.process_queue_task.done():
            self.process_queue_task.cancel()

    async def process_queue(self) -> None:
        """Task to process the queue in the background."""
        while True:
            try:
                if not self.connected:
                    self.async_empty_queue_and_cancel_tasks()
                    return

                char_id, payload = await self._queue.get()
                await self._write_msg(char_id, payload)
                self._queue.task_done()
                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                self.connected = False
                return
            except (BookooDeviceNotFound, BookooError) as ex:
                self.connected = False
                _LOGGER.debug("Error writing to device: %s", ex)
                return

    async def connect(
        self,
        callback: (
            Callable[[BleakGATTCharacteristic, bytearray], Awaitable[None] | None]
            | None
        ) = None,
        setup_tasks: bool = True,
    ) -> None:
        """Connect the bluetooth client."""

        if self.connected:
            return

        if self.last_disconnect_time and self.last_disconnect_time > (time.time() - 5):
            _LOGGER.debug(
                "Scale has recently been disconnected, waiting 5 seconds before reconnecting"
            )
            return

        self._client = BleakClient(
            address_or_ble_device=self.address_or_ble_device,
            disconnected_callback=self.device_disconnected_handler,
        )

        try:
            await self._client.connect()
        except BleakError as ex:
            msg = "Error during connecting to device"
            _LOGGER.debug("%s: %s", msg, ex)
            raise BookooError(msg) from ex
        except TimeoutError as ex:
            msg = "Timeout during connecting to device"
            _LOGGER.debug("%s: %s", msg, ex)
            raise BookooError(msg) from ex
        except Exception as ex:
            msg = "Unknown error during connecting to device"
            _LOGGER.debug("%s: %s", msg, ex)
            raise BookooError(msg) from ex

        self.connected = True
        _LOGGER.debug("Connected to Bookoo scale")

        if callback is None:
            callback = self.on_bluetooth_data_received
        try:
            await self._client.start_notify(
                char_specifier=self._weight_char_id,
                callback=(
                    self.on_bluetooth_data_received if callback is None else callback
                ),
            )
            await asyncio.sleep(0.1)
        except BleakError as ex:
            msg = "Error subscribing to notifications"
            _LOGGER.debug("%s: %s", msg, ex)
            raise BookooError(msg) from ex

        if setup_tasks:
            self._setup_tasks()

    def _setup_tasks(self) -> None:
        """Set up background tasks."""
        if not self.process_queue_task or self.process_queue_task.done():
            self.process_queue_task = asyncio.create_task(self.process_queue())

    async def disconnect(self) -> None:
        """Clean disconnect from the scale."""

        _LOGGER.debug("Disconnecting from scale")
        self.connected = False
        await self._queue.join()
        if not self._client:
            return
        try:
            await self._client.disconnect()
        except BleakError as ex:
            _LOGGER.debug("Error disconnecting from device: %s", ex)
        else:
            _LOGGER.debug("Disconnected from scale")

    async def tare(self) -> None:
        """Tare the scale."""
        if not self.connected:
            await self.connect()
        async with self._add_to_queue_lock:
            await self._queue.put((self._command_char_id, self._msg_types["tare"]))

    async def start_timer(self) -> None:
        """Start the timer."""
        if not self.connected:
            await self.connect()

        _LOGGER.debug('Sending "start" message')

        async with self._add_to_queue_lock:
            await self._queue.put(
                (self._command_char_id, self._msg_types["startTimer"])
            )

    async def stop_timer(self) -> None:
        """Stop the timer."""
        if not self.connected:
            await self.connect()

        _LOGGER.debug('Sending "stop" message')

        async with self._add_to_queue_lock:
            await self._queue.put((self._command_char_id, self._msg_types["stopTimer"]))

    async def tare_and_start_timer(self) -> None:
        """Tare and Start the timer."""
        if not self.connected:
            await self.connect()

        _LOGGER.debug('Sending "tare and start" message')

        async with self._add_to_queue_lock:
            await self._queue.put(
                (self._command_char_id, self._msg_types["tareAndStartTime"])
            )

    async def reset_timer(self) -> None:
        """Reset the timer."""
        if not self.connected:
            await self.connect()

        _LOGGER.debug('Sending "reset" message')

        async with self._add_to_queue_lock:
            await self._queue.put(
                (self._command_char_id, self._msg_types["resetTimer"])
            )

    async def on_bluetooth_data_received(
        self,
        characteristic: BleakGATTCharacteristic,  # pylint: disable=unused-argument
        data: bytearray,
    ) -> None:
        """Receive data from scale."""

        # _LOGGER.debug("Received data: %s", ",".join(f"{byte:02x}" for byte in data))

        try:
            msg, _ = decode(data)
        except BookooMessageTooShort as ex:
            _LOGGER.debug("Non-header message too short: %s", ex.bytes_recvd)
            return
        except BookooMessageTooLong as ex:
            _LOGGER.debug("%s: %s", ex.message, ex.bytes_recvd)
            return
        except BookooMessageError as ex:
            _LOGGER.warning("%s: %s", ex.message, ex.bytes_recvd)
            return

        if isinstance(msg, BookooMessage):
            self._weight = msg.weight
            self._timer = msg.timer
            self._flow_rate = msg.flow_rate
            self._device_state = BookooDeviceState(
                battery_level=msg.battery,
                units=UnitMass("grams"),
                buzzer_gear=msg.buzzer_gear,
                auto_off_time=msg.standby_time,
            )

        if self._notify_callback is not None:
            self._notify_callback()
