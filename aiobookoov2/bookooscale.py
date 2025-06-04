"""Client to interact with Bookoo scales."""

from __future__ import annotations  # noqa: I001

import asyncio
import logging
import time

from collections.abc import Callable

from dataclasses import dataclass

from bleak import BleakClient, BleakGATTCharacteristic, BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

from .const import (
    CHARACTERISTIC_UUID_WEIGHT,
    CHARACTERISTIC_UUID_COMMAND,
    UPDATE_SOURCE_WEIGHT_CHAR,
    UPDATE_SOURCE_COMMAND_CHAR,
    UnitMass,
    CMD_TARE,  # Import new command constants
    CMD_START_TIMER,
    CMD_STOP_TIMER,
    CMD_RESET_TIMER,
    CMD_TARE_AND_START_TIMER,
)
from .exceptions import (
    BookooDeviceNotFound,
    BookooError,
    BookooMessageError,
    BookooMessageTooLong,
    BookooMessageTooShort,
)
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

    # _command_payloads will store the mapping from command name to its base byte sequence (without checksum)
    # It will be initialized in __init__ using constants.

    def _calculate_checksum(self, payload_without_checksum: bytes) -> int:
        """Calculate the XOR checksum for a given payload."""
        checksum = 0
        for byte_val in payload_without_checksum:
            checksum ^= byte_val
        return checksum

    def __init__(
        self,
        address_or_ble_device: str | BLEDevice,
        name: str | None = None,
        is_valid_scale: bool = True,
        notify_callback: Callable[[], None] | None = None,  # General state update
        characteristic_update_callback: Callable[[str, bytes | dict | None], None]
        | None = None,  # Detailed char data
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
        self._characteristic_update_callback: (
            Callable[[str, bytes | dict | None], None] | None
        ) = characteristic_update_callback

        # Initialize command payloads using imported constants
        # These are base payloads; checksum will be calculated in async_send_command
        self._command_base_payloads = {
            "tare": CMD_TARE[:-1],  # Exclude checksum byte from const
            "start_timer": CMD_START_TIMER[:-1],
            "stop_timer": CMD_STOP_TIMER[:-1],
            "reset_timer": CMD_RESET_TIMER[:-1],
            "tare_and_start_timer": CMD_TARE_AND_START_TIMER[:-1],
        }

    async def _add_to_queue(self, char_id: str, payload: bytearray) -> None:
        """Add a message to the queue to be sent to the device."""
        if not self._client or not self.connected:
            _LOGGER.error("Cannot send command, not connected to %s", self.mac)
            raise BookooError(f"Not connected to device {self.mac}")
        async with self._add_to_queue_lock:
            await self._queue.put((char_id, payload))
            _LOGGER.debug("Added to queue for %s: %s", char_id, payload.hex())

    async def async_send_command(self, command_name: str) -> None:
        """Send a command to the scale by name."""
        if command_name not in self._command_base_payloads:
            _LOGGER.error("Unknown command name: %s", command_name)
            raise BookooError(f"Unknown command: {command_name}")

        base_payload = self._command_base_payloads[command_name]
        checksum = self._calculate_checksum(
            base_payload
        )  # Checksum is calculated on the base payload
        full_payload = bytearray(base_payload)  # Create mutable bytearray from base
        full_payload.append(checksum)  # Append the calculated checksum

        _LOGGER.debug(
            "Sending command '%s' with payload %s (base: %s, checksum: %02x)",
            command_name,
            full_payload.hex(),
            base_payload.hex(),
            checksum,
        )
        await self._add_to_queue(self._command_char_id, full_payload)

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
    def timer(self) -> float | None:
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
        # Schedule the async cleanup task as this handler is synchronous
        # If the task is already being handled or is done, maybe don't reschedule.
        if self.process_queue_task and not self.process_queue_task.done():
            try:
                loop = asyncio.get_running_loop()
                if not loop.is_closed():  # Check if loop is not already closing
                    # Schedule the cleanup, but don't necessarily wait for it here
                    # as this handler is a callback.
                    _LOGGER.debug(
                        "Scheduling async_empty_queue_and_cancel_tasks via loop.create_task for %s",
                        self.mac,
                    )
                    loop.create_task(self.async_empty_queue_and_cancel_tasks())
                else:
                    _LOGGER.warning(
                        "Event loop closed in device_disconnected_handler for %s, synchronous cancel attempt for process_queue_task.",
                        self.mac,
                    )
                    if (
                        self.process_queue_task and not self.process_queue_task.done()
                    ):  # Check again before cancelling
                        self.process_queue_task.cancel()
            except RuntimeError:  # Loop not running
                _LOGGER.warning(
                    "No running event loop in device_disconnected_handler for %s. Synchronous cancel attempt for process_queue_task.",
                    self.mac,
                )
                if (
                    self.process_queue_task and not self.process_queue_task.done()
                ):  # Check again before cancelling
                    self.process_queue_task.cancel()
        elif self.process_queue_task and self.process_queue_task.done():
            _LOGGER.debug(
                "process_queue_task for %s already done in device_disconnected_handler.",
                self.mac,
            )
        else:
            _LOGGER.debug(
                "No process_queue_task or task already None in device_disconnected_handler for %s.",
                self.mac,
            )

        if notify and self._notify_callback:
            try:
                loop = asyncio.get_running_loop()
                if not loop.is_closed():
                    self._notify_callback()
                else:
                    _LOGGER.debug(
                        "Event loop closed, skipping _notify_callback in device_disconnected_handler for %s",
                        self.mac,
                    )
            except RuntimeError:
                _LOGGER.debug(
                    "No running event loop, skipping _notify_callback in device_disconnected_handler for %s",
                    self.mac,
                )

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

    async def async_empty_queue_and_cancel_tasks(self) -> None:
        """Empty the queue and ensure the processing task is cancelled and awaited."""

        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

        task_to_await = self.process_queue_task
        if task_to_await and not task_to_await.done():
            _LOGGER.debug("Cancelling process_queue_task for %s", self.mac)
            task_to_await.cancel()
            try:
                _LOGGER.debug("Awaiting cancelled process_queue_task for %s", self.mac)
                await task_to_await
                _LOGGER.debug("Cancelled process_queue_task for %s finished.", self.mac)
            except asyncio.CancelledError:
                _LOGGER.debug(
                    "process_queue_task for %s was cancelled as expected.", self.mac
                )
            except Exception as e:
                _LOGGER.error(
                    "Exception while awaiting cancelled process_queue_task for %s: %s",
                    self.mac,
                    e,
                    exc_info=True,
                )
        self.process_queue_task = None  # Clear the reference

    async def process_queue(self) -> None:
        """Task to process the queue in the background."""
        try:
            while True:
                if not self.connected:
                    _LOGGER.debug(
                        "process_queue for %s: not connected, initiating cleanup.",
                        self.mac,
                    )
                    await self.async_empty_queue_and_cancel_tasks()
                    return

                char_id, payload = await self._queue.get()
                _LOGGER.debug(
                    "process_queue for %s: got item, writing %s to %s",
                    self.mac,
                    payload.hex(),
                    char_id,
                )
                await self._write_msg(char_id, payload)
                self._queue.task_done()
                await asyncio.sleep(
                    0.1
                )  # Small delay to prevent busy-looping on rapid queue additions

        except asyncio.CancelledError:
            _LOGGER.debug(
                "process_queue for %s received CancelledError. Cleaning up.", self.mac
            )
            # This task is being cancelled. The entity that cancelled it
            # (e.g. async_empty_queue_and_cancel_tasks) should await its completion.
            # Setting self.connected = False is important.
            self.connected = False
            # Do not call async_empty_queue_and_cancel_tasks from here if this task is being cancelled by it,
            # to avoid potential recursion if the await in that method re-enters here somehow before this task fully exits.
            # The primary canceller should handle the await.
            raise  # Re-raise CancelledError so awaiter knows task is cancelled
        except (BookooDeviceNotFound, BookooError) as ex:
            _LOGGER.debug(
                "process_queue for %s: connection error: %s. Cleaning up.", self.mac, ex
            )
            await self.async_empty_queue_and_cancel_tasks()
            self.connected = False
            # Do not re-raise, allow task to exit gracefully after error
        except Exception as ex:
            _LOGGER.error(
                "process_queue for %s: unexpected error: %s. Cleaning up.",
                self.mac,
                ex,
                exc_info=True,
            )
            await self.async_empty_queue_and_cancel_tasks()
            self.connected = False
            # Do not re-raise, allow task to exit gracefully after error
        finally:
            _LOGGER.debug("process_queue for %s is exiting.", self.mac)

    async def _internal_notification_handler(
        self, sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notifications from both weight and command characteristics."""
        # _LOGGER.debug("Notification from %s: %s (raw)", sender.uuid, data.hex())

        decoded_payload, _ = decode(
            data
        )  # decode returns (BookooMessage|dict|None, remaining_bytes)

        if sender.uuid == self._weight_char_id:
            if isinstance(decoded_payload, BookooMessage):
                msg = decoded_payload
                self._weight = msg.weight
                self._timer = msg.timer  # Already in seconds
                self._flow_rate = msg.flow_rate

                current_unit = (
                    UnitMass.GRAMS
                )  # Default, as per protocol docs 0x2b is grams
                if msg.unit == 0x2B:  # Explicitly grams
                    current_unit = UnitMass.GRAMS
                # Add other unit mappings here if the scale can send other units via this byte

                if self._device_state is None:
                    self._device_state = BookooDeviceState(
                        battery_level=msg.battery,
                        units=current_unit,
                        buzzer_gear=msg.buzzer_gear,
                        auto_off_time=0,  # Initialize, actual auto_off_time is set by command, not from weight notification
                    )
                else:
                    self._device_state.battery_level = msg.battery
                    self._device_state.units = current_unit
                    self._device_state.buzzer_gear = msg.buzzer_gear

                # _LOGGER.debug("BookooScale state updated: W:%.2f, T:%.2f, FR:%.2f, Batt:%d, Unit:%s",
                #               self._weight or 0, self._timer or 0, self._flow_rate or 0,
                #               self._device_state.battery_level, self._device_state.units)

                if self._characteristic_update_callback:
                    # Notify coordinator that weight data was processed. Coordinator will read new state from self.scale attributes.
                    self._characteristic_update_callback(
                        UPDATE_SOURCE_WEIGHT_CHAR, None
                    )  # Pass None as data, coordinator reads from self.scale
                if (
                    self._notify_callback
                ):  # General state change notification for HA listeners
                    self._notify_callback()
            else:
                _LOGGER.debug(
                    "Weight char data did not decode to BookooMessage. Got: %s. Raw: %s",
                    type(decoded_payload).__name__,
                    data.hex(),
                )
                # Optionally, still notify coordinator with raw data if needed for debugging in HA
                # if self._characteristic_update_callback:
                #     self._characteristic_update_callback(UPDATE_SOURCE_WEIGHT_CHAR, bytes(data))

        elif sender.uuid == self._command_char_id:
            # For command characteristic, we expect a dict (e.g., for auto-timer) or None
            if self._characteristic_update_callback:
                if isinstance(decoded_payload, dict):
                    # Pass the decoded dictionary directly to the coordinator
                    self._characteristic_update_callback(
                        UPDATE_SOURCE_COMMAND_CHAR, decoded_payload
                    )
                else:
                    # If not a dict (e.g. None or other), pass raw data for coordinator to inspect/log
                    _LOGGER.debug(
                        "Command char data did not decode to dict. Got: %s. Raw: %s",
                        type(decoded_payload).__name__ if decoded_payload else "None",
                        data.hex(),
                    )
                    self._characteristic_update_callback(
                        UPDATE_SOURCE_COMMAND_CHAR, bytes(data)
                    )
            if self._notify_callback:
                self._notify_callback()  # Notify for command char updates too

        else:
            _LOGGER.warning(
                "Notification from unexpected characteristic %s: %s",
                sender.uuid,
                data.hex(),
            )
            if self._characteristic_update_callback:
                # Pass raw data for unknown characteristics
                self._characteristic_update_callback("unknown_char_update", bytes(data))
            if self._notify_callback:
                self._notify_callback()

    async def connect(
        self,
        setup_tasks: bool = True,
    ) -> None:
        """Connect the bluetooth client."""
        _LOGGER.debug("Connecting to %s", self.mac)
        try:
            if self._client and self._client.is_connected:
                _LOGGER.debug("Already connected to %s", self.mac)
                return

            self._client = BleakClient(
                self.address_or_ble_device,
                disconnected_callback=self.device_disconnected_handler,
            )
            await self._client.connect()
            self.connected = self._client.is_connected
            _LOGGER.debug("Connected to %s: %s", self.mac, self.connected)

            if not self.connected:
                raise BookooDeviceNotFound(f"Failed to connect to {self.mac}")

            # Subscribe to notifications
            await self._client.start_notify(
                self._weight_char_id, self._internal_notification_handler
            )
            _LOGGER.debug(
                "Subscribed to weight characteristic (%s) notifications.",
                self._weight_char_id,
            )

            try:
                await self._client.start_notify(
                    self._command_char_id, self._internal_notification_handler
                )
                _LOGGER.debug(
                    "Subscribed to command characteristic (%s) notifications.",
                    self._command_char_id,
                )
            except Exception as e:
                _LOGGER.warning(
                    "Could not subscribe to command characteristic (%s) notifications: %s. "
                    "Auto-timer events from scale will not be detected.",
                    self._command_char_id,
                    e,
                )

            if setup_tasks:
                if self.process_queue_task and not self.process_queue_task.done():
                    self.process_queue_task.cancel()
                self.process_queue_task = asyncio.create_task(self.process_queue())
                _LOGGER.debug("Queue processing task started for %s", self.mac)

            if self._notify_callback:
                self._notify_callback()

        except BleakDeviceNotFoundError as ex:
            self.connected = False
            self.last_disconnect_time = time.time()
            raise BookooDeviceNotFound(f"Device {self.mac} not found") from ex
        except BleakError as ex:
            self.connected = False
            self.last_disconnect_time = time.time()
            _LOGGER.error("BleakError while connecting to %s: %s", self.mac, ex)
            raise BookooError(f"Error connecting to {self.mac}") from ex
        except Exception as ex:
            self.connected = False
            self.last_disconnect_time = time.time()
            _LOGGER.error(
                "Unexpected error connecting to %s: %s", self.mac, ex, exc_info=True
            )
            raise BookooError(f"Unexpected error connecting to {self.mac}") from ex

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
            _LOGGER.debug("Error during BLE disconnect: %s", ex)
        # Ensure tasks are cleaned up regardless of disconnect success
        await (
            self.async_empty_queue_and_cancel_tasks()
        )  # Now awaiting the async version
        _LOGGER.debug("Finished disconnect procedure for scale")

    async def tare(self) -> None:
        """Send tare command to the scale."""
        await self.async_send_command("tare")

    async def start_timer(self) -> None:
        """Send start timer command to the scale."""
        await self.async_send_command("start_timer")

    async def stop_timer(self) -> None:
        """Send stop timer command to the scale."""
        await self.async_send_command("stop_timer")

    async def reset_timer(self) -> None:
        """Send reset timer command to the scale."""
        await self.async_send_command("reset_timer")

    async def tare_and_start_timer(self) -> None:
        """Send tare and start timer command to the scale."""
        await self.async_send_command("tare_and_start_timer")

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
