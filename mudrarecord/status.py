"""Parser for the Mudra Band firmware status notifications.

The device answers ``get_general_status`` with a notification whose first byte
is ``0x01`` and ``get_airmouse_status`` with one whose first byte is ``0x02``.
mudrarecord only reads the few fields it needs to display device info and to
verify that HID routing is off and the requested channels are enabled.
"""
from __future__ import annotations

from .protocol import AirMouseStatusIndex as AMI
from .protocol import GeneralStatusIndex as GSI


class FirmwareStatus:
    """Holds the most recent general and air-mouse status byte arrays."""

    def __init__(self) -> None:
        self.general: list[int] | None = None
        self.airmouse: list[int] | None = None

    def update(self, data: bytes) -> None:
        if not data:
            return
        if data[0] == 0x01:
            self.general = list(data)
        elif data[0] == 0x02:
            self.airmouse = list(data)

    def _gen(self, index: int) -> int:
        if self.general and index < len(self.general):
            return self.general[index]
        return 0

    def _air(self, index: int) -> int:
        if self.airmouse and index < len(self.airmouse):
            return self.airmouse[index]
        return 0

    @property
    def is_snc_enabled(self) -> bool:
        return self._gen(GSI.SNC) == 1

    @property
    def is_acc_enabled(self) -> bool:
        return self._gen(GSI.ACC) == 1

    @property
    def is_gyro_enabled(self) -> bool:
        return self._gen(GSI.GYRO) == 1

    @property
    def is_gesture_enabled(self) -> bool:
        return self._gen(GSI.GESTURE) == 1

    @property
    def is_navigation_enabled(self) -> bool:
        return self._gen(GSI.NAVIGATION) == 1

    @property
    def is_gesture_to_hid_enabled(self) -> bool:
        return self._air(AMI.GESTURE_TO_HID) == 1

    @property
    def is_nav_to_hid_enabled(self) -> bool:
        return self._air(AMI.NAV_TO_HID) == 1

    @property
    def is_nav_to_app_enabled(self) -> bool:
        return self._air(AMI.NAV_TO_APP) == 1

    @property
    def hand(self) -> str:
        return "right" if self._air(AMI.HAND) == 1 else "left"

    @property
    def band_mode(self) -> str:
        return "mudra_link" if self._air(AMI.BAND_MODE) == 1 else "mudra_band"
