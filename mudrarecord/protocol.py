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

* SNC characteristic (``0000fff4``), 112 bytes = 56 int16 LE:
      indices 0..53  : 18 samples, 3 channels interleaved (snc1, snc2, snc3)
      indices 54..55 : 2-value footer (not sensor data)
  Deinterleaving by ``index % 3`` yields three smooth oscillating sEMG
  waveforms (the three nerve electrodes). The sentinels -32768 / +32767 mark
  clipped/invalid samples (railed channel or poor electrode contact). See the
  SNC section further down for details; RMS is computed per packet/channel.
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


# --- SNC (sEMG) decode -----------------------------------------------------
#
# Reverse-engineered from a live Mudra Band (firmware 6.0.11.5). The vendor
# software decodes SNC inside a closed-source native library that has no Linux
# build, so the layout below was derived by analysing the raw BLE packets.
#
# A full SNC notification is 112 bytes = 56 signed 16-bit little-endian values:
#
#   * indices 0..53  : 18 samples, 3 channels interleaved column-major, i.e.
#                      sample_i = (snc1_i, snc2_i, snc3_i). Consecutive
#                      same-channel values form a smooth oscillating waveform.
#   * indices 54..55 : a 2-value footer (not sensor data).
#
# The extreme int16 values -32768 (0x8000) and +32767 (0x7fff) are clip/invalid
# sentinels the firmware emits when a channel is railed or has poor electrode
# contact; they are excluded from RMS and reported as NaN in the float output.
#
# The 3 SNC channels correspond to the three nerve electrodes (SNC1/SNC2/SNC3 in
# the vendor SDK). RMS is not transmitted; the native SDK derives it, so
# mudrarecord computes a per-packet RMS per channel over that packet's valid
# (non-sentinel) samples.

SNC_TYPE_TAG = 0x2E
SNC_N_CHANNELS = 3
SNC_SAMPLES_PER_PACKET = 18       # per channel
SNC_DATA_INT16 = SNC_N_CHANNELS * SNC_SAMPLES_PER_PACKET  # 54
SNC_CLIP_MIN = -32768
SNC_CLIP_MAX = 32767


def _snc_valid(v: int) -> bool:
    return v != SNC_CLIP_MIN and v != SNC_CLIP_MAX


def decode_snc_channels(data: bytes) -> List[List[float]]:
    """Decode an SNC packet into per-sample rows of 3 channel float values.

    Returns a list of ``[snc1, snc2, snc3]`` float rows (one per interleaved
    sample in the packet). Clipped/invalid sentinel values are returned as
    ``float('nan')`` so downstream tools can distinguish "no signal" from a real
    zero reading. Returns [] for packets that are too short.
    """
    if len(data) < SNC_DATA_INT16 * 2:
        return []
    n_int16 = min(len(data) // 2, SNC_DATA_INT16)
    values = struct.unpack_from("<%dh" % n_int16, data, 0)
    rows: list[list[float]] = []
    for i in range(0, (n_int16 // SNC_N_CHANNELS) * SNC_N_CHANNELS, SNC_N_CHANNELS):
        row = [
            float(v) if _snc_valid(v) else float("nan")
            for v in values[i : i + SNC_N_CHANNELS]
        ]
        rows.append(row)
    return rows


def snc_packet_rms(data: bytes) -> List[float]:
    """Compute per-channel RMS over one SNC packet's valid samples.

    Returns three floats ``[rms1, rms2, rms3]``. A channel with no valid samples
    in the packet yields ``float('nan')``. This is a per-packet value: the same
    RMS triple applies to every linearized sample row emitted from the packet.
    """
    rows = decode_snc_channels(data)
    rms: list[float] = []
    for c in range(SNC_N_CHANNELS):
        vals = [row[c] for row in rows if row[c] == row[c]]  # drop NaN
        if vals:
            rms.append((sum(v * v for v in vals) / len(vals)) ** 0.5)
        else:
            rms.append(float("nan"))
    return rms


def decode_snc_samples(data: bytes) -> List[int]:
    """Deprecated flat decode kept for reference (channel 1 only).

    Prefer :func:`decode_snc_channels`. Returns the first SNC channel as ints.
    """
    return [int(row[0]) if row[0] == row[0] else 0 for row in decode_snc_channels(data)]


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
