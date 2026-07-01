"""Mudra Band BLE protocol: UUIDs, feature indices, and raw packet decoders.

This module is self-contained and does not depend on any external Mudra library.
It documents only what mudrarecord needs to record raw channels.

Two raw data characteristics are relevant:

* SNC  (``0000fff4``): the raw sEMG / surface-nerve-conductance stream.
* IMU  (``0000fff5``): the raw inertial stream (accelerometer and/or gyroscope).

Packet layouts (reverse-engineered from a live Mudra Band, firmware 6.0.11.5;
the vendor software decodes these inside a closed-source native library, so
mudrarecord always keeps the verbatim raw bytes alongside the decoded values):

* IMU characteristic (``0000fff5``), packet type byte ``0x04``, ~106 bytes:
      byte 0        : type tag (0x04)
      bytes 1..48   : 8 accelerometer samples, each 3x int16 LE (ax, ay, az)
      bytes 49..52  : 4-byte counter / footer (not decoded)
      bytes 53..    : 8 gyroscope records, each 6 bytes = [int16 counter][float32]
  The accelerometer block decodes cleanly and reproducibly. The gyroscope
  block layout is NOT reliably understood, so mudrarecord does not decode it into
  channels; the raw bytes are still preserved in the recorded raw hex. The IMU
  packet always carries both acc and gyro regardless of which enable commands
  were sent.

* SNC characteristic (``0000fff4``), packet type byte ``0x2e``, ~112 bytes:
      byte 0..1     : header
      then repeating: [int16 LE sample][0x00 0x80 marker]
  The int16 values immediately preceding each ``00 80`` marker form the sEMG
  waveform. This has been observed to produce a clean oscillating signal, but
  the channel/scaling semantics are not officially documented; treat as
  EXPERIMENTAL and rely on the raw hex column for anything authoritative.
"""
from __future__ import annotations

import struct
from typing import List


class Characteristic:
    """BLE GATT characteristic UUIDs used by mudrarecord."""

    SNC = "0000fff4-0000-1000-8000-00805f9b34fb"
    IMU = "0000fff5-0000-1000-8000-00805f9b34fb"
    COMMAND = "0000fff1-0000-1000-8000-00805f9b34fb"
    MESSAGE = "0000fff6-0000-1000-8000-00805f9b34fb"
    BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
    CHARGING_STATE = "00002a1a-0000-1000-8000-00805f9b34fb"
    FIRMWARE_VERSION = "00002a26-0000-1000-8000-00805f9b34fb"
    SERIAL_RIGHT = "00002a25-0000-1000-8000-00805f9b34fb"
    SERIAL_LEFT = "00002a27-0000-1000-8000-00805f9b34fb"


# Device name filter used during discovery.
DEVICE_NAME_FILTER = "mudra"

CHARGING_TRUE = 0x7B


class GeneralStatusIndex:
    """Byte offsets into the general-status notification (byte[0] == 0x01)."""

    IMU_QUATERNION = 2
    ACC = 3
    GYRO = 4
    NAVIGATION = 12
    GESTURE = 13
    SNC = 19
    PRESSURE = 23


class AirMouseStatusIndex:
    """Byte offsets into the air-mouse-status notification (byte[0] == 0x02)."""

    NAV_TO_HID = 2
    NAV_TO_APP = 3
    GESTURE_TO_HID = 4
    MAPPER_MODE = 5
    HAND = 9
    BAND_MODE = 17


# --- Raw IMU decode --------------------------------------------------------

IMU_TYPE_TAG = 0x04
IMU_ACC_OFFSET = 1          # accelerometer block starts here
IMU_ACC_SAMPLE_SIZE = 6     # 3 axes * int16
IMU_ACC_N_SAMPLES = 8       # samples per packet
IMU_GYRO_OFFSET = 53        # gyroscope block starts here
IMU_GYRO_RECORD_SIZE = 6    # [int16 counter][float32]
IMU_GYRO_N_RECORDS = 8


def decode_acc_samples(data: bytes) -> List[tuple[int, int, int]]:
    """Decode the accelerometer block of an IMU (type 0x04) packet.

    Returns up to ``IMU_ACC_N_SAMPLES`` (ax, ay, az) int16 triplets. Verified
    against live hardware. Returns [] if the packet is too short or not type
    0x04.
    """
    if len(data) < IMU_ACC_OFFSET + IMU_ACC_SAMPLE_SIZE or data[0] != IMU_TYPE_TAG:
        return []
    samples: list[tuple[int, int, int]] = []
    off = IMU_ACC_OFFSET
    for _ in range(IMU_ACC_N_SAMPLES):
        if off + IMU_ACC_SAMPLE_SIZE > len(data):
            break
        samples.append(struct.unpack_from("<3h", data, off))
        off += IMU_ACC_SAMPLE_SIZE
    return samples


def decode_gyro_floats(data: bytes) -> List[float]:
    """Decode the gyroscope block of an IMU (type 0x04) packet.

    EXPERIMENTAL / UNRELIABLE: interprets each 6-byte gyro record as
    ``[int16 counter][float32 value]``. On live hardware this does NOT reliably
    produce sensible values, so mudrarecord does not use it for output; the raw
    gyro bytes are preserved in the packet's raw hex instead. Kept for
    exploration/reverse-engineering only.
    """
    if len(data) < IMU_GYRO_OFFSET + IMU_GYRO_RECORD_SIZE or data[0] != IMU_TYPE_TAG:
        return []
    out: list[float] = []
    off = IMU_GYRO_OFFSET
    for _ in range(IMU_GYRO_N_RECORDS):
        if off + IMU_GYRO_RECORD_SIZE > len(data):
            break
        # record = [int16 counter][float32 value]
        out.append(struct.unpack_from("<f", data, off + 2)[0])
        off += IMU_GYRO_RECORD_SIZE
    return out


def decode_imu_packet(data: bytes) -> List[tuple[int, int, int]]:
    """Decode a type-0x04 IMU packet into accelerometer (ax, ay, az) samples.

    Returns one verified int16 accelerometer triplet per sample in the packet.
    The gyroscope block is intentionally not decoded (its layout is not
    reliably understood); use the raw packet bytes if you need it. Returns []
    for non-0x04 / short packets.
    """
    return [tuple(s) for s in decode_acc_samples(data)]


# --- Raw SNC decode --------------------------------------------------------

SNC_TYPE_TAG = 0x2E
SNC_MARKER = b"\x00\x80"


def decode_snc_samples(data: bytes) -> List[int]:
    """Decode the SNC (sEMG) packet into a list of int16 samples.

    Each sample is the int16 immediately preceding a ``00 80`` marker. Verified
    to yield a clean oscillating waveform on live hardware, but the channel /
    scaling semantics are not officially documented (treat as experimental).
    """
    samples: list[int] = []
    n = len(data)
    i = 2  # skip 2-byte header
    while i + 2 <= n:
        if data[i] == 0x00 and data[i + 1] == 0x80 and i >= 2:
            samples.append(struct.unpack_from("<h", data, i - 2)[0])
        i += 1
    return samples


# --- Small scalar parsers --------------------------------------------------

def parse_battery(data: bytes) -> int:
    return data[0] if data else 0


def parse_charging(data: bytes) -> bool:
    return bool(data) and data[0] == CHARGING_TRUE


def parse_firmware_version(data: bytes) -> str:
    return data.decode("utf-8", errors="ignore")


def parse_serial_part(data: bytes, side: str) -> int:
    try:
        value = int(data.decode("utf-8", errors="ignore"), 16)
    except (ValueError, UnicodeDecodeError):
        value = 0
    return value * 1_000_000 if side == "left" else value
