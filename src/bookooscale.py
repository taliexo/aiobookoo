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
        max_queue_size: int = 100,  # Max items in command queue
        queue_process_delay: float = 0.1,  # Delay between queue item processing
        max_connect_attempts: int = 3,  # Max attempts for initial connection
        initial_retry_delay: float = 1.0,  # Initial delay for connection retry in seconds
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
        self._queue_process_delay = queue_process_delay
        self._max_connect_attempts = max_connect_attempts
        self._initial_retry_delay = initial_retry_delay

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
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
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

        # Schedule the asynchronous task to empty queue and cancel processing tasks.
        # This is preferred over complex synchronous logic in a callback.
        try:
            # The async_empty_queue_and_cancel_tasks will handle clearing the queue
            # and cancelling the process_queue_task.
            asyncio.create_task(self.async_empty_queue_and_cancel_tasks())
            _LOGGER.debug(
                "Scheduled async_empty_queue_and_cancel_tasks for %s.", self.mac
            )
        except RuntimeError as e:
            _LOGGER.error(
                "Failed to schedule async_empty_queue_and_cancel_tasks for %s due to RuntimeError: %s. "
                "Queue might not be emptied and task might not be cancelled if event loop is not running.",
                self.mac,
                e,
            )
            # Fallback: If create_task fails (e.g. loop not running), attempt direct cancellation.
            # This is a best-effort and might have issues if the task is in an uninterruptible await.
            if self.process_queue_task and not self.process_queue_task.done():
                _LOGGER.warning(
                    "Attempting direct cancellation of process_queue_task for %s as a fallback.",
                    self.mac,
                )
                self.process_queue_task.cancel()

        if notify and self._notify_callback:
            self._notify_callback()

        # Reset client. This should be done after attempting to cancel tasks
        # that might use the client, though Bleak typically handles this.
        self._client = None
        _LOGGER.info("Scale %s disconnected.", self.mac)

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
        self.process_queue_task = None  # Ensure task is cleared after handling

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
                if not self._client:
                    _LOGGER.error(
                        "process_queue for %s: No BleakClient available to write payload %s to %s.",
                        self.mac,
                        payload.hex(),
                        char_id,
                    )
                    # Handle as a connection error, trigger cleanup
                    raise BookooError(f"BLE client not available for device {self.mac}")

                await self._client.write_gatt_char(
                    char_id, payload, response=True
                )  # Assuming response=True is desired
                self._timestamp_last_command = time.time()
                _LOGGER.debug("Successfully wrote to %s: %s", char_id, payload.hex())
                self._queue.task_done()
                await asyncio.sleep(
                    self._queue_process_delay
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
        except (
            BookooDeviceNotFound,
            BookooError,
            BleakError,
            asyncio.TimeoutError,
        ) as ex:  # Consolidate BLE/Connection errors
            # Try to get char_id and payload if available in this scope, otherwise log general error
            # For now, we'll assume they are not reliably available here if the error happened outside the get/write sequence.
            _LOGGER.warning(
                "process_queue for %s: BLE/connection error: %s (%s). Cleaning up.",
                self.mac,
                type(ex).__name__,
                ex,
            )
            await self.async_empty_queue_and_cancel_tasks()
            self.connected = False
            # Do not re-raise, allow task to exit gracefully after error
        except Exception as ex:  # Catch-all for other unexpected errors
            _LOGGER.error(
                "process_queue for %s: Unexpected error: %s (%s). Cleaning up.",
                self.mac,
                type(ex).__name__,
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
                        UPDATE_SOURCE_COMMAND_CHAR,
                        dict(decoded_payload),  # Cast TypedDict to dict
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
        attempts: int | None = None,  # Allow overriding connection attempts
    ) -> None:
        """Connect the bluetooth client with retry logic."""
        last_exception: Exception | None = None

        # Determine the number of connection attempts
        num_attempts_to_try: int
        if attempts is not None:
            num_attempts_to_try = max(
                1, attempts
            )  # Ensure at least 1 attempt if specified
        else:
            num_attempts_to_try = self._max_connect_attempts

        for attempt_num in range(num_attempts_to_try):
            _LOGGER.debug(
                "Connecting to %s (Attempt %d/%d)",
                self.mac,
                attempt_num + 1,
                num_attempts_to_try,
            )
            try:
                if self._client and self._client.is_connected:
                    _LOGGER.debug("Already connected to %s", self.mac)
                    return  # Already connected

                # Ensure client is fresh for each attempt if previous one failed
                if self._client:
                    await self._client.disconnect()  # Ensure cleanup
                self._client = BleakClient(
                    self.address_or_ble_device,
                    disconnected_callback=self.device_disconnected_handler,
                )

                await self._client.connect()
                self.connected = self._client.is_connected
                _LOGGER.info("Connected to %s: %s", self.mac, self.connected)

                if not self.connected:
                    raise BookooDeviceNotFound(
                        f"Failed to connect to {self.mac} after connect call, but no exception from Bleak."
                    )

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
                except (
                    Exception
                ) as e_notify_cmd:  # Catch specific exceptions if possible
                    _LOGGER.warning(
                        "Could not subscribe to command characteristic (%s) notifications on %s: %s. "
                        "Auto-timer events from scale will not be detected.",
                        self._command_char_id,
                        self.mac,
                        e_notify_cmd,
                    )

                if setup_tasks:
                    self._setup_tasks()

                _LOGGER.info(
                    "Successfully connected to %s after %d attempt(s).",
                    self.mac,
                    attempt_num + 1,
                )
                return  # Successful connection

            except (
                BleakError,
                asyncio.TimeoutError,
            ) as e:  # Covers BleakDeviceNotFoundError
                _LOGGER.debug(
                    "Connection attempt %d/%d to %s failed: %s (%s)",
                    attempt_num + 1,
                    num_attempts_to_try,
                    self.mac,
                    type(e).__name__,
                    e,
                )
                self.connected = False
                last_exception = e
                if attempt_num + 1 < num_attempts_to_try:
                    delay = self._initial_retry_delay * (2**attempt_num)
                    _LOGGER.info(
                        "Retrying connection to %s in %.2f seconds...", self.mac, delay
                    )
                    await asyncio.sleep(delay)
                else:
                    _LOGGER.debug(
                        "All %d connection attempts to %s failed.",
                        num_attempts_to_try,
                        self.mac,
                    )
            except (
                Exception
            ) as e:  # Catch any other unexpected errors during this specific attempt
                _LOGGER.error(
                    "Unexpected error during connection attempt %d/%d to %s: %s (%s)",
                    attempt_num + 1,
                    num_attempts_to_try,
                    self.mac,
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
                self.connected = False
                last_exception = e  # Store it to be raised if all retries fail
                break  # Break from retry loop for truly unexpected errors not related to BLE availability

        # If loop finishes without successful return, all retries failed
        if last_exception:
            if isinstance(last_exception, BleakDeviceNotFoundError):
                raise BookooDeviceNotFound(
                    f"Device {self.mac} not found after {num_attempts_to_try} attempts"
                ) from last_exception
            elif isinstance(last_exception, BleakError):  # Catches other BleakErrors
                raise BookooError(
                    f"Connection error for {self.mac} after {num_attempts_to_try} attempts: {last_exception}"
                ) from last_exception
            elif isinstance(last_exception, asyncio.TimeoutError):
                raise BookooError(
                    f"Connection timeout for {self.mac} after {num_attempts_to_try} attempts"
                ) from last_exception
            else:
                raise BookooError(
                    f"Unexpected connection error for {self.mac} after {num_attempts_to_try} attempts: {last_exception}"
                ) from last_exception
        elif not self.connected:
            # Fallback if no exception was stored but connection failed
            raise BookooDeviceNotFound(
                f"Failed to connect to {self.mac} after {num_attempts_to_try} attempts, reason unknown."
            )

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
        """Ensure background tasks, like queue processing, are created and running if not already active."""
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
        """Handle raw data received from Bluetooth characteristics (deprecated).

        This method is kept for compatibility or specific low-level scenarios.
        The primary data handling is done via _internal_notification_handler.
        """

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
