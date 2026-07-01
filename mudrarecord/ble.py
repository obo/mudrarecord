"""Low-level BLE connection management for the Mudra Band.

Self-contained wrapper over ``bleak``. The key feature for mudrarecord is that a
global wall-clock nanosecond timestamp (``time.time_ns()``) is captured inside
the notification dispatcher, as early as possible after a packet arrives, and
handed to the registered handler together with the payload.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .protocol import Characteristic, DEVICE_NAME_FILTER

logger = logging.getLogger("mudrarecord.ble")

# (uuid, timestamp_ns, data)
NotifyHandler = Callable[[str, int, bytearray], None]

DBUS_SOCKET = "/run/dbus/system_bus_socket"


def _ensure_dbus() -> None:
    """Point bleak at the system D-Bus socket if it exists (Linux/BlueZ)."""
    if os.path.exists(DBUS_SOCKET):
        os.environ.setdefault(
            "DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={DBUS_SOCKET}"
        )


class MudraNotFoundError(RuntimeError):
    pass


class MudraConnectionError(RuntimeError):
    pass


class BleConnection:
    """Manages a BLE connection to a single Mudra Band device."""

    def __init__(self, post_connect_delay: float = 1.5) -> None:
        self._client: Optional[BleakClient] = None
        self._device: Optional[BLEDevice] = None
        self._handlers: dict[str, NotifyHandler] = {}
        self._on_disconnect: Optional[Callable[[], None]] = None
        self.post_connect_delay = post_connect_delay
        self._subscribed: set[str] = set()

    # --- Discovery ---

    @staticmethod
    async def _find_bonded() -> list[BLEDevice]:
        """Look up bonded Mudra devices directly in BlueZ (no scan required).

        Bonded devices often use rotating random addresses that a passive scan
        will not surface, so consult the BlueZ object manager first.
        """
        _ensure_dbus()
        try:
            from bleak.backends.bluezdbus.manager import get_global_bluez_manager
        except ImportError:
            return []

        try:
            manager = await get_global_bluez_manager()
        except Exception as e:  # pragma: no cover - platform dependent
            logger.debug("BlueZ manager unavailable: %s", e)
            return []

        devices: list[BLEDevice] = []
        for path, all_props in manager._properties.items():
            props = all_props.get("org.bluez.Device1")
            if not props:
                continue
            name = props.get("Name", "")
            if name and DEVICE_NAME_FILTER in name.lower():
                devices.append(
                    BLEDevice(
                        address=props.get("Address", ""),
                        name=name,
                        details={"path": path, "props": props},
                        rssi=props.get("RSSI", -50),
                    )
                )
        return devices

    @staticmethod
    async def _scan(timeout: float) -> list[BLEDevice]:
        _ensure_dbus()
        devices: list[BLEDevice] = []
        seen: set[str] = set()

        def callback(device: BLEDevice, _adv):
            if device.name and DEVICE_NAME_FILTER in device.name.lower():
                if device.address not in seen:
                    seen.add(device.address)
                    devices.append(device)

        scanner = BleakScanner(detection_callback=callback)
        await scanner.start()
        await asyncio.sleep(timeout)
        await scanner.stop()
        return devices

    @staticmethod
    async def discover(timeout: float = 10.0) -> list[BLEDevice]:
        """Return Mudra devices: bonded first, then a fresh scan."""
        bonded = await BleConnection._find_bonded()
        if bonded:
            return bonded
        return await BleConnection._scan(timeout)

    # --- Connection ---

    async def connect(self, device: BLEDevice) -> None:
        _ensure_dbus()
        self._device = device
        logger.info("Connecting to %s (%s)", device.name, device.address)
        target = device if device.details else device.address
        self._client = BleakClient(
            target, disconnected_callback=self._handle_disconnect
        )
        try:
            await self._client.connect()
        except Exception as e:
            self._client = None
            raise MudraConnectionError(
                f"Failed to connect to {getattr(device, 'name', None) or 'device'} "
                f"({device.address}): {e}\n"
                f"Make sure the band is powered on and in range. For bonded "
                f"devices try: bluetoothctl connect {device.address}"
            ) from e

        if self.post_connect_delay > 0:
            await asyncio.sleep(self.post_connect_delay)

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception as e:  # pragma: no cover
                logger.debug("Error during disconnect: %s", e)
        self._client = None
        self._subscribed.clear()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_on_disconnect(self, callback: Callable[[], None]) -> None:
        self._on_disconnect = callback

    # --- Notifications ---

    def on_notify(self, char_uuid: str, handler: NotifyHandler) -> None:
        """Register a handler; call before ``start_notify``."""
        self._handlers[char_uuid] = handler

    async def start_notify(self, char_uuid: str) -> None:
        """Subscribe to notifications on a characteristic (idempotent)."""
        assert self._client is not None
        if char_uuid in self._subscribed:
            return
        cb = lambda _sender, data, uuid=char_uuid: self._dispatch(uuid, data)
        try:
            await self._client.start_notify(char_uuid, cb)
        except Exception as e1:
            logger.debug("AcquireNotify failed for %s: %s", char_uuid[-4:], e1)
            await self._client.start_notify(
                char_uuid, cb, bluez={"use_start_notify": True}
            )
        self._subscribed.add(char_uuid)

    async def stop_notify(self, char_uuid: str) -> None:
        if self._client and char_uuid in self._subscribed:
            try:
                await self._client.stop_notify(char_uuid)
            except Exception as e:  # pragma: no cover
                logger.debug("stop_notify failed for %s: %s", char_uuid[-4:], e)
            self._subscribed.discard(char_uuid)

    def _dispatch(self, char_uuid: str, data: bytearray) -> None:
        # Capture the arrival timestamp as early as possible.
        ts_ns = time.time_ns()
        handler = self._handlers.get(char_uuid)
        if handler is None:
            return
        try:
            handler(char_uuid, ts_ns, data)
        except Exception:
            logger.exception("Error in notification handler for %s", char_uuid[-4:])

    # --- Read / write ---

    async def read(self, char_uuid: str, timeout: float = 10.0) -> bytes:
        if not self._client:
            raise MudraConnectionError("Not connected")
        return bytes(
            await asyncio.wait_for(self._client.read_gatt_char(char_uuid), timeout)
        )

    async def write(self, char_uuid: str, data: bytes, timeout: float = 10.0) -> None:
        if not self._client:
            raise MudraConnectionError("Not connected")
        await asyncio.wait_for(
            self._client.write_gatt_char(char_uuid, data), timeout
        )

    def _handle_disconnect(self, _client) -> None:
        logger.info("Device disconnected")
        if self._on_disconnect:
            self._on_disconnect()
