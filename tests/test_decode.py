import pytest
import logging # For caplog
from unittest.mock import patch, MagicMock

from aiobookoov2.decode import decode, BookooMessage
from aiobookoov2.const import (
    WEIGHT_BYTE1,
    WEIGHT_BYTE2,
    CMD_BYTE1_PRODUCT_NUMBER,
    CMD_BYTE2_MESSAGE_TYPE_AUTO_TIMER,
    CMD_BYTE3_AUTO_TIMER_EVENT_START,
    CMD_BYTE3_AUTO_TIMER_EVENT_STOP
)
from aiobookoov2.exceptions import BookooMessageError # For completeness, though decode might return None now

# Helper to calculate checksum
def calculate_checksum(payload_without_checksum: bytes) -> int:
    checksum = 0
    for byte_val in payload_without_checksum:
        checksum ^= byte_val
    return checksum

# Test Payloads (examples, might need refinement based on full protocol)
VALID_WEIGHT_PAYLOAD_NO_CS = bytearray([
    WEIGHT_BYTE1, WEIGHT_BYTE2, 0x01, # Indices 0, 1, 2. Prefix, status/flag
    0x00, 0x00, # Timer (0 ms) - Indices 3, 4
    0x00,       # Unit (grams) - Index 5
    0x2B,       # Weight Symbol (+) - Index 6
    0x00,       # Reserved - Index 7 (Skipped by BookooMessage parsing for main fields)
    0x03, 0xE8, # Weight (10.00g -> 1000) - Indices 8, 9
    0x2B,       # Flow Symbol (+) - Index 10
    0x00,       # Reserved - Index 11 (Skipped by BookooMessage parsing for main fields)
    0x64,       # Flow rate (1.00 ml/s -> 100, assuming 1 byte for this example) - Index 12
    0x64,       # Battery (100%) - Index 13
    0x05,       # Standby time (5 min) - Index 14
    0x00,       # Reserved - Index 15 (Skipped by BookooMessage parsing for main fields)
    0x01,       # Buzzer gear (1) - Index 16
    0x00,       # Flow rate smoothing (off) - Index 17
    0x00,       # Reserved - Index 18 (Not used by BookooMessage parsing for main fields)
    0xAA        # Placeholder for checksum (20th byte, Index 19) - value doesn't matter here
])
calculated_weight_cs = calculate_checksum(VALID_WEIGHT_PAYLOAD_NO_CS[:-1])
VALID_WEIGHT_PAYLOAD = VALID_WEIGHT_PAYLOAD_NO_CS[:-1] + bytes([calculated_weight_cs])

AUTO_TIMER_START_PAYLOAD_NO_CS = bytearray([
    CMD_BYTE1_PRODUCT_NUMBER, CMD_BYTE2_MESSAGE_TYPE_AUTO_TIMER, CMD_BYTE3_AUTO_TIMER_EVENT_START,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 # Padding
])
AUTO_TIMER_START_PAYLOAD = AUTO_TIMER_START_PAYLOAD_NO_CS[:-1] + bytes([calculate_checksum(AUTO_TIMER_START_PAYLOAD_NO_CS[:-1])])

AUTO_TIMER_STOP_PAYLOAD_NO_CS = bytearray([
    CMD_BYTE1_PRODUCT_NUMBER, CMD_BYTE2_MESSAGE_TYPE_AUTO_TIMER, CMD_BYTE3_AUTO_TIMER_EVENT_STOP,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 # Padding
])
AUTO_TIMER_STOP_PAYLOAD = AUTO_TIMER_STOP_PAYLOAD_NO_CS[:-1] + bytes([calculate_checksum(AUTO_TIMER_STOP_PAYLOAD_NO_CS[:-1])])


class TestAioBookooDecode:
    def test_decode_valid_weight_message(self, caplog): # Add caplog
        caplog.set_level(logging.DEBUG, logger="aiobookoo.decode") # Set level
        msg, remaining = decode(VALID_WEIGHT_PAYLOAD)
        assert isinstance(msg, BookooMessage)
        assert msg.weight == 10.00
        assert msg.timer == 0.0
        assert msg.flow_rate == 1.00 # Example value
        assert msg.battery == 100
        assert len(remaining) == 0

    def test_decode_auto_timer_start(self):
        msg, remaining = decode(AUTO_TIMER_START_PAYLOAD)
        assert msg == {'type': 'auto_timer', 'event': 'start'}
        assert len(remaining) == 0

    def test_decode_auto_timer_stop(self):
        msg, remaining = decode(AUTO_TIMER_STOP_PAYLOAD)
        assert msg == {'type': 'auto_timer', 'event': 'stop'}
        assert len(remaining) == 0

    def test_decode_weight_checksum_error(self):
        invalid_payload = bytearray(VALID_WEIGHT_PAYLOAD)
        invalid_payload[-1] = (invalid_payload[-1] + 1) % 256 # Corrupt checksum
        msg, remaining = decode(invalid_payload)
        assert msg is None
        assert remaining == invalid_payload

    def test_decode_auto_timer_checksum_error(self):
        invalid_payload = bytearray(AUTO_TIMER_START_PAYLOAD)
        invalid_payload[-1] = (invalid_payload[-1] + 1) % 256 # Corrupt checksum
        msg, remaining = decode(invalid_payload)
        assert msg is None
        assert remaining == invalid_payload

    def test_decode_unknown_message_type(self):
        unknown_payload_no_cs = bytearray([
            0xFF, 0xFF, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 # 20 bytes
        ])
        unknown_payload = unknown_payload_no_cs[:-1] + bytes([calculate_checksum(unknown_payload_no_cs[:-1])])
        msg, remaining = decode(unknown_payload)
        assert msg is None
        assert remaining == unknown_payload

    def test_decode_too_short_message(self):
        short_payload = bytearray([0x03, 0x0B, 0x01]) # Too short to be anything known
        # The decode function no longer raises BookooMessageTooShort for generic short messages
        # It will fall through and return (None, original_message)
        msg, remaining = decode(short_payload)
        assert msg is None
        assert remaining == short_payload

    def test_decode_known_cmd_prefix_unknown_event(self):
        unknown_event_payload_no_cs = bytearray([
            CMD_BYTE1_PRODUCT_NUMBER, CMD_BYTE2_MESSAGE_TYPE_AUTO_TIMER, 0x02, # Unknown event byte
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
        ])
        unknown_event_payload = unknown_event_payload_no_cs[:-1] + bytes([calculate_checksum(unknown_event_payload_no_cs[:-1])])
        msg, remaining = decode(unknown_event_payload)
        assert msg is None # decode.py logs this but returns None for the message part
        assert remaining == unknown_event_payload
