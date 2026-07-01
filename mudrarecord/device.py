"""High-level Mudra Band controller for recording raw channels.

Responsibilities:

* discover / connect / disconnect
* read device info (battery, firmware, serial)
* disable HID output so the band does NOT act as a mouse/keyboard while recording
* enable exactly the raw channels the user asked for (skipping the rest saves
  BLE bandwidth on the device, which can raise the achievable rate of the
  remaining channels)
* deliver raw SNC / IMU packets to callbacks together with a global ns timestamp
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from bleak.backends.device import BLEDevice

from .ble import BleConnection, MudraNotFoundError
from .commands import Command, FirmwareTarget, SampleType
from .protocol import (
    Characteristic,
    parse_battery,
    parse_charging,
    parse_firmware_version,
    parse_serial_part,
)
from .status import FirmwareStatus

logger = logging.getLogger("mudrarecord.device")

# (timestamp_ns, data) callback for a raw stream.
RawCallback = Callable[[int, bytes], None]


class MudraBand:
    """Controller for a single Mudra Band device."""

    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device
        self._conn = BleConnection()
        self.status = FirmwareStatus()

        self.battery_level: int = 0
        self.is_charging: bool = False
        self.firmware_version: str = ""
        self.serial_number: int = 0

        self._on_raw_snc: Optional[RawCallback] = None
        self._on_raw_imu: Optional[RawCallback] = None
        self._on_disconnect: Optional[Callable[[], None]] = None

    # --- Discovery ---

    @staticmethod
    async def discover(timeout: float = 10.0) -> "MudraBand":
        devices = await BleConnection.discover(timeout)
        if not devices:
            raise MudraNotFoundError(
                "No Mudra Band devices found. Make sure the band is powered on. "
                "For bonded devices, check: bluetoothctl devices"
            )
        return MudraBand(devices[0])

    @staticmethod
    def from_address(address: str, name: str = "Mudra Band") -> "MudraBand":
        return MudraBand(BLEDevice(address=address, name=name, details=None))

    # --- Properties ---

    @property
    def name(self) -> str:
        return self._ble_device.name or "Unknown"

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        return self._conn.is_connected

    # --- Callbacks ---

    def on_raw_snc(self, func: RawCallback) -> RawCallback:
        self._on_raw_snc = func
        return func

    def on_raw_imu(self, func: RawCallback) -> RawCallback:
        self._on_raw_imu = func
        return func

    def on_disconnect(self, func: Callable[[], None]) -> Callable[[], None]:
        self._on_disconnect = func
        return func

    # --- Connection ---

    async def connect(self) -> None:
        await self._conn.connect(self._ble_device)
        self._conn.set_on_disconnect(self._handle_disconnect)

        # Command notifications carry status responses.
        self._conn.on_notify(Characteristic.COMMAND, self._handle_command)
        self._conn.on_notify(Characteristic.SNC, self._handle_snc)
        self._conn.on_notify(Characteristic.IMU, self._handle_imu)

        await self._conn.start_notify(Characteristic.COMMAND)

        await self._read_device_info()
        await self.refresh_status()
        await asyncio.sleep(0.3)

    async def disconnect(self) -> None:
        await self._conn.disconnect()

    async def _read_device_info(self) -> None:
        for char, parser, attr in [
            (Characteristic.BATTERY, parse_battery, "battery_level"),
            (Characteristic.CHARGING_STATE, parse_charging, "is_charging"),
            (Characteristic.FIRMWARE_VERSION, parse_firmware_version, "firmware_version"),
        ]:
            try:
                setattr(self, attr, parser(await self._conn.read(char)))
            except Exception as e:
                logger.debug("Failed to read %s: %s", attr, e)

        serial = 0
        for char, side in [
            (Characteristic.SERIAL_LEFT, "left"),
            (Characteristic.SERIAL_RIGHT, "right"),
        ]:
            try:
                serial += parse_serial_part(await self._conn.read(char), side)
            except Exception as e:
                logger.debug("Failed to read serial %s: %s", side, e)
        self.serial_number = serial

    # --- Status ---

    async def refresh_status(self) -> None:
        await self._write(Command.get_general_status())
        await self._write(Command.get_airmouse_status())

    # --- HID control ---

    async def disable_hid(self) -> None:
        """Stop the band from acting as a BLE mouse/keyboard while recording.

        Turns off gesture->HID and nav->HID routing, and also nav->app so the
        firmware is not spending bandwidth on navigation output either.
        """
        await self._write(Command.set_firmware_target(FirmwareTarget.GESTURE_TO_HID, False))
        await asyncio.sleep(0.05)
        await self._write(Command.set_firmware_target(FirmwareTarget.NAV_TO_HID, False))
        await asyncio.sleep(0.05)
        await self._write(Command.set_firmware_target(FirmwareTarget.NAV_TO_APP, False))
        await asyncio.sleep(0.05)

    async def enable_hid(self) -> None:
        """Re-enable gesture->HID routing (used by --restore-hid on exit)."""
        await self._write(Command.set_firmware_target(FirmwareTarget.GESTURE_TO_HID, True))

    # --- Sample type ---

    async def set_sample_type(self, sample_type: str) -> None:
        mapping = {"16bit": SampleType.BIT_16, "24bit": SampleType.BIT_24}
        if sample_type not in mapping:
            raise ValueError("sample_type must be '16bit' or '24bit'")
        await self._write(Command.set_sample_type(mapping[sample_type]))
        await asyncio.sleep(0.1)

    # --- Channel enable/disable ---

    async def enable_snc(self) -> None:
        await self._write(Command.enable_snc())

    async def disable_snc(self) -> None:
        await self._write(Command.disable_snc())

    async def enable_imu_acc(self) -> None:
        await self._write(Command.enable_imu_acc())

    async def disable_imu_acc(self) -> None:
        await self._write(Command.disable_imu_acc())

    async def enable_imu_gyro(self) -> None:
        await self._write(Command.enable_imu_gyro())

    async def disable_imu_gyro(self) -> None:
        await self._write(Command.disable_imu_gyro())

    async def start_snc_notify(self) -> None:
        await self._conn.start_notify(Characteristic.SNC)

    async def start_imu_notify(self) -> None:
        await self._conn.start_notify(Characteristic.IMU)

    # --- Streaming helper ---

    async def stream_forever(self) -> None:
        """Block until the device disconnects."""
        while self.is_connected:
            await asyncio.sleep(0.2)

    # --- Internal ---

    async def _write(self, data: bytes) -> None:
        await self._conn.write(Characteristic.COMMAND, data)

    def _handle_command(self, _uuid: str, _ts_ns: int, data: bytearray) -> None:
        if data and data[0] in (0x01, 0x02):
            self.status.update(bytes(data))

    def _handle_snc(self, _uuid: str, ts_ns: int, data: bytearray) -> None:
        if self._on_raw_snc:
            self._on_raw_snc(ts_ns, bytes(data))

    def _handle_imu(self, _uuid: str, ts_ns: int, data: bytearray) -> None:
        if self._on_raw_imu:
            self._on_raw_imu(ts_ns, bytes(data))

    def _handle_disconnect(self) -> None:
        if self._on_disconnect:
            self._on_disconnect()
