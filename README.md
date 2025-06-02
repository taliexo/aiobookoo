# aiobookoov2

**aiobookoov2** is an asynchronous Python library for interacting with Bookoo Themis Mini coffee scales over Bluetooth Low Energy (BLE). It is based on the `aioacaia` library and utilizes `asyncio` and `bleak` for its operations.

This library is primarily designed to be used by the [Bookoo Home Assistant integration](https://github.com/taliexo/bookoo) but can also be used as a standalone library for other projects.



## Features

*   Connect to Bookoo Themis Mini scales.
*   Receive real-time weight, timer, and flow rate data.
*   Send commands to the scale:
    *   Tare
    *   Start Timer
    *   Stop Timer
    *   Reset Timer
    *   Tare & Start Timer
*   Handle notifications from the scale, including:
    *   Weight characteristic updates (0xFF11).
    *   Command characteristic updates (0xFF12), specifically for 0x0D auto-timer start/stop events.
*   Decode BLE messages into a structured format.
*   Provides callbacks for characteristic data updates and general state changes.

## Requirements

*   Python 3.12+
*   `bleak>=0.20.2`
*   A Bluetooth adapter compatible with `bleak` on your system.

## Installation

You can install `aiobookoov2` directly from PyPI:

```bash
pip install aiobookoov2
```

To install for development:
```bash
git clone https://github.com/taliexo/aiobookoo.git
cd aiobookoo
pip install -e .[dev]
```

## Basic Usage

Here's a simple example of how to connect to a scale and receive updates:

```python
import asyncio
import logging
from bleak import BleakScanner
from aiobookoov2 import BookooScale, UPDATE_SOURCE_WEIGHT_CHAR, UPDATE_SOURCE_COMMAND_CHAR

# Configure logging
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)
# For more detailed bleak/aiobookoov2 logs, set level to DEBUG:
# logging.getLogger("aiobookoov2").setLevel(logging.DEBUG)
# logging.getLogger("bleak").setLevel(logging.DEBUG)


TARGET_SCALE_ADDRESS = "XX:XX:XX:XX:XX:XX"  # Replace with your scale's MAC address
# Or discover automatically:
# from aiobookoov2.helpers import find_bookoo_devices
# async def discover():
#     devices = await find_bookoo_devices(timeout=10.0)
#     if devices:
#         return devices[0].address
#     return None
# TARGET_SCALE_ADDRESS = asyncio.run(discover())


def my_characteristic_update_handler(source: str, data: bytes | dict | None):
    """Handle characteristic data updates."""
    if source == UPDATE_SOURCE_WEIGHT_CHAR:
        # Data for weight char is usually None, scale object has updated attributes
        _LOGGER.info(
            f"Weight update: Weight={scale.weight}g, Timer={scale.timer}s, Flow={scale.flow_rate}g/s, Battery={scale.device_state.battery_level if scale.device_state else 'N/A'}%"
        )
    elif source == UPDATE_SOURCE_COMMAND_CHAR:
        _LOGGER.info(f"Command char update: {data}")
        if isinstance(data, dict) and data.get("type") == "auto_timer":
            _LOGGER.info(f"Scale auto-timer event: {data.get('event')}")


async def main():
    global scale
    if not TARGET_SCALE_ADDRESS:
        _LOGGER.error("Scale address not set. Please discover or hardcode it.")
        return

    scale = BookooScale(
        address_or_ble_device=TARGET_SCALE_ADDRESS,
        characteristic_update_callback=my_characteristic_update_handler,
    )

    try:
        _LOGGER.info(f"Attempting to connect to {TARGET_SCALE_ADDRESS}...")
        if await scale.async_connect():
            _LOGGER.info("Connected! Waiting for notifications...")
            # Keep the script running to receive notifications
            # You can send commands here, e.g.:
            # await asyncio.sleep(5)
            # await scale.async_send_command("tare")
            # _LOGGER.info("Tare command sent.")
            while scale.connected:
                await asyncio.sleep(1)
        else:
            _LOGGER.error("Failed to connect.")
    except Exception as e:
        _LOGGER.error(f"An error occurred: {e}")
    finally:
        if scale.connected:
            _LOGGER.info("Disconnecting...")
            await scale.async_disconnect()
            _LOGGER.info("Disconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _LOGGER.info("Program stopped by user.")
```

## Development

This project uses `ruff` for linting and formatting, and `mypy` for static type checking. Pre-commit hooks are configured to run these checks automatically.

To set up for development:
1.  Install `pre-commit`: `pipx install pre-commit` or `brew install pre-commit`.
2.  Install hooks: `pre-commit install`.

Run checks manually:
*   `ruff check .`
*   `ruff format .`
*   `mypy --config-file=mypy.ini aiobookoov2/`
*   `pytest tests/`

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
