"""Output sinks for Mudra Band data: CSV and LSL.

Design goals:

* Fast and lightweight: the BLE notification callback does the minimum work
  (format one line / push one LSL sample) and returns immediately.
* Runs for hours: CSV output is streamed through a buffered writer and can be
  written to a file until the disk fills up (a disk-full error is reported and
  recording stops cleanly). Nothing is accumulated in memory.
* Exact global timestamps: every emitted sample carries the wall-clock
  ``time.time_ns()`` captured when its BLE packet arrived, plus a nanosecond
  ISO-8601 UTC string in the CSV.

Output is *primarily real decoded values* (floats). The verbatim raw packet
bytes are only included when explicitly requested (``include_raw=True`` /
``--include-raw-bytes``).

Decoded channel layout per stream (fixed so CSV columns are stable):

* ``snc``: the three sEMG channels ``snc1, snc2, snc3`` plus the per-packet
  RMS of each channel ``rms1, rms2, rms3``. One BLE packet contains a batch of
  samples; the packet is linearized into one row per sample, and the packet's
  RMS triple is repeated on every row of that packet.
* ``imu``: the accelerometer axes ``ax, ay, az`` (int16). The gyroscope portion
  of the IMU packet is not decoded; enable ``include_raw`` to keep its bytes.
"""
from __future__ import annotations

import datetime as _dt
import math
import sys
from typing import Optional, TextIO

from . import protocol

# CSV value columns (floats), in fixed order across all rows.
SNC_COLUMNS = ["snc1", "snc2", "snc3", "rms1", "rms2", "rms3"]
IMU_COLUMNS = ["ax", "ay", "az"]
VALUE_COLUMNS = SNC_COLUMNS + IMU_COLUMNS

# LSL channel layouts.
SNC_LSL_CHANNELS = 6  # snc1, snc2, snc3, rms1, rms2, rms3
IMU_LSL_CHANNELS = 3  # ax, ay, az


def _iso_ns(ts_ns: int) -> str:
    """Format a ns wall-clock timestamp as ISO-8601 UTC with nanoseconds."""
    secs, ns = divmod(ts_ns, 1_000_000_000)
    dt = _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{ns:09d}Z"


def _fmt(v) -> str:
    """Format a decoded channel value compactly for CSV."""
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        return repr(v)
    return str(v)


class Decimator:
    """Keeps at most one out of every ``factor`` samples per stream.

    ``factor == 1`` (the default / "max") keeps everything.
    """

    def __init__(self, factor: int = 1) -> None:
        self.factor = max(1, int(factor))
        self._counts: dict[str, int] = {}

    def accept(self, stream: str) -> bool:
        if self.factor == 1:
            return True
        c = self._counts.get(stream, 0)
        self._counts[stream] = c + 1
        return c % self.factor == 0


class CSVSink:
    """Streams one CSV row per decoded sample to a file or stdout.

    Columns (raw_hex only present when ``include_raw`` is set)::

        timestamp_ns, timestamp_iso, stream, [raw_hex,]
        snc1, snc2, snc3, rms1, rms2, rms3, ax, ay, az

    Each row fills only the columns relevant to its ``stream`` (``snc`` or
    ``imu``); the others are left empty.
    """

    _STREAM_COLUMNS = {
        "snc": SNC_COLUMNS,
        "imu": IMU_COLUMNS,
    }

    def __init__(
        self, path: Optional[str], flush_every: int = 200, include_raw: bool = False
    ) -> None:
        self._path = path
        self._flush_every = max(1, flush_every)
        self._include_raw = include_raw
        self._fh: Optional[TextIO] = None
        self._owns_fh = False
        self._since_flush = 0
        self.count = 0
        self._col_index = {c: i for i, c in enumerate(VALUE_COLUMNS)}

    def open(self) -> None:
        if self._path in (None, "-"):
            self._fh = sys.stdout
            self._owns_fh = False
        else:
            # Large write buffer keeps the per-packet write() cheap; suitable
            # for hours of continuous recording.
            self._fh = open(self._path, "w", buffering=1 << 20, newline="")
            self._owns_fh = True
        header = ["timestamp_ns", "timestamp_iso", "stream"]
        if self._include_raw:
            header.append("raw_hex")
        header += VALUE_COLUMNS
        self._fh.write(",".join(header) + "\n")

    def write(self, stream: str, ts_ns: int, raw: bytes, channels: list) -> None:
        assert self._fh is not None
        values = [""] * len(VALUE_COLUMNS)
        for col, val in zip(self._STREAM_COLUMNS.get(stream, []), channels):
            values[self._col_index[col]] = _fmt(val)
        row = [str(ts_ns), _iso_ns(ts_ns), stream]
        if self._include_raw:
            row.append(raw.hex())
        row += values
        self._fh.write(",".join(row))
        self._fh.write("\n")
        self.count += 1
        self._since_flush += 1
        if self._since_flush >= self._flush_every:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception:
                pass
            if self._owns_fh:
                self._fh.close()
            self._fh = None


class LSLSink:
    """Pushes samples to Lab Streaming Layer outlets (one per stream).

    Two outlets are created lazily as data arrives:

    * ``Mudra-SNC`` (type ``EMG``): 6 channels ``snc1, snc2, snc3, rms1, rms2,
      rms3``. The three sEMG signals plus the per-packet RMS of each channel.
    * ``Mudra-IMU`` (type ``Motion``): 3 channels ``ax, ay, az`` (accelerometer).

    All channels use the ``float32`` LSL format. Nominal sampling rate is
    advertised as irregular because the device streams at its own (variable)
    native rate; each sample is pushed with the exact wall-clock timestamp
    captured at BLE arrival, converted to LSL seconds. Channel labels are
    written to the stream metadata so tools such as MNE-Python and OpenViBE
    display named channels.
    """

    def __init__(self, source_id: str) -> None:
        self._source_id = source_id
        self._outlets: dict[str, object] = {}
        self._lsl = None
        self.count = 0

    def open(self) -> None:
        try:
            import pylsl  # noqa: F401
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "pylsl is required for LSL output. Install it with: "
                'pip install "mudrarecord[lsl]"'
            ) from e
        self._lsl = pylsl

    def _get_outlet(self, stream: str, n_channels: int):
        outlet = self._outlets.get(stream)
        if outlet is not None:
            return outlet
        pylsl = self._lsl
        assert pylsl is not None

        if stream == "snc":
            name, stype, labels = "Mudra-SNC", "EMG", list(SNC_COLUMNS)
        else:
            name, stype, labels = "Mudra-IMU", "Motion", list(IMU_COLUMNS)

        info = pylsl.StreamInfo(
            name=name,
            type=stype,
            channel_count=n_channels,
            nominal_srate=pylsl.IRREGULAR_RATE,
            channel_format=pylsl.cf_float32,
            source_id=f"{self._source_id}-{stream}",
        )
        chans = info.desc().append_child("channels")
        for label in labels[:n_channels]:
            ch = chans.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "raw")
            ch.append_child_value("type", stype)
        outlet = pylsl.StreamOutlet(info)
        self._outlets[stream] = outlet
        return outlet

    def write(self, stream: str, ts_ns: int, raw: bytes, channels: list) -> None:
        n = SNC_LSL_CHANNELS if stream == "snc" else IMU_LSL_CHANNELS
        sample = [float(c) for c in channels[:n]]
        sample += [0.0] * (n - len(sample))
        outlet = self._get_outlet(stream, n)
        outlet.push_sample(sample, ts_ns / 1_000_000_000.0)  # type: ignore[attr-defined]
        self.count += 1

    def close(self) -> None:
        # Outlets are torn down when garbage-collected; drop references.
        self._outlets.clear()


def decode_samples(stream: str, raw: bytes) -> list[list]:
    """Decode a raw packet into one or more per-sample channel lists.

    * ``snc``: one row per interleaved sample, each
      ``[snc1, snc2, snc3, rms1, rms2, rms3]`` where the RMS triple is the
      per-packet RMS of each channel (identical on every row of the packet).
    * ``imu``: one row per accelerometer sample, each ``[ax, ay, az]``.
    """
    if stream == "snc":
        rows = protocol.decode_snc_channels(raw)
        if not rows:
            return []
        rms = protocol.snc_packet_rms(raw)
        return [row + rms for row in rows]
    return [list(row) for row in protocol.decode_imu_packet(raw)]
