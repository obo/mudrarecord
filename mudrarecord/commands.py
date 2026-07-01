"""Firmware command byte sequences written to the COMMAND characteristic.

Only the commands mudrarecord needs are included. All are written to
``Characteristic.COMMAND`` (``0000fff1``).

3-byte enable/disable commands follow the pattern
``[feature_group, sub_index, enable_flag]`` where ``feature_group`` is
``0x06`` for the SNC/pressure family and ``0x07`` for the IMU family, and
``enable_flag`` is ``0x01`` (enable) or ``0x00`` (disable).
"""
from __future__ import annotations


class Command:
    """Static builders returning raw command byte sequences."""

    # --- Raw sensor streams ---
    @staticmethod
    def enable_snc() -> bytes:
        return bytes([0x06, 0x00, 0x01])

    @staticmethod
    def disable_snc() -> bytes:
        return bytes([0x06, 0x00, 0x00])

    @staticmethod
    def enable_imu_acc() -> bytes:
        return bytes([0x07, 0x03, 0x01])

    @staticmethod
    def disable_imu_acc() -> bytes:
        return bytes([0x07, 0x03, 0x00])

    @staticmethod
    def enable_imu_gyro() -> bytes:
        return bytes([0x07, 0x02, 0x01])

    @staticmethod
    def disable_imu_gyro() -> bytes:
        return bytes([0x07, 0x02, 0x00])

    # --- Sample type (16-bit vs 24-bit) ---
    @staticmethod
    def set_sample_type(sample_type: int) -> bytes:
        # 0 = 16-bit, 1 = 24-bit
        return bytes([0x22, sample_type & 0xFF])

    # --- HID routing (send firmware output to nav-app / nav-hid / gesture-hid) ---
    @staticmethod
    def set_firmware_target(target: int, active: bool) -> bytes:
        # target: 0 = nav_to_app, 1 = nav_to_hid, 2 = gesture_to_hid
        return bytes([0x55, target & 0xFF, 0x01 if active else 0x00])

    # --- Status queries ---
    @staticmethod
    def get_general_status() -> bytes:
        return bytes([0x75, 0x01])

    @staticmethod
    def get_airmouse_status() -> bytes:
        return bytes([0x75, 0x02])


class FirmwareTarget:
    NAV_TO_APP = 0
    NAV_TO_HID = 1
    GESTURE_TO_HID = 2


class SampleType:
    BIT_16 = 0
    BIT_24 = 1
