import struct

from mudrarecord.recorder import CSVSink, Decimator, _iso_ns, decode_samples


def test_iso_ns_preserves_nanoseconds():
    ts = 1_700_000_000 * 1_000_000_000 + 123456789
    s = _iso_ns(ts)
    assert s.endswith(".123456789Z")
    assert s.startswith("2023-11-14T")


def test_decimator_max_keeps_all():
    d = Decimator(1)
    assert all(d.accept("snc") for _ in range(10))


def test_decimator_factor_keeps_one_in_n():
    d = Decimator(3)
    kept = [d.accept("imu") for _ in range(9)]
    assert kept == [True, False, False, True, False, False, True, False, False]


def test_decimator_is_per_stream():
    d = Decimator(2)
    assert d.accept("snc") is True
    assert d.accept("imu") is True
    assert d.accept("snc") is False
    assert d.accept("imu") is False


def _imu_packet(acc_samples, gyro_floats):
    from mudrarecord import protocol

    buf = bytearray([protocol.IMU_TYPE_TAG])
    for ax, ay, az in acc_samples:
        buf += struct.pack("<3h", ax, ay, az)
    while len(buf) < protocol.IMU_GYRO_OFFSET:
        buf.append(0)
    for i, g in enumerate(gyro_floats):
        buf += struct.pack("<h", i) + struct.pack("<f", g)
    return bytes(buf)


def _snc_packet(rows):
    """Build a 112-byte SNC packet from a list of [c1,c2,c3] int rows (18)."""
    from mudrarecord import protocol

    vals = []
    for r in rows:
        vals += list(r)
    while len(vals) < protocol.SNC_DATA_INT16:
        vals.append(protocol.SNC_CLIP_MAX)
    vals += [0, 0]  # footer
    return struct.pack("<%dh" % len(vals), *vals)


def test_decode_samples_imu_expands_per_sample():
    pkt = _imu_packet([(1, 2, 3), (4, 5, 6)], [0.5, 0.25])
    out = decode_samples("imu", pkt)
    assert out[0] == [1, 2, 3]
    assert out[1] == [4, 5, 6]


def test_decode_samples_snc_channels_and_rms():
    # Two samples of 3 channels, rest padded with clip sentinel.
    pkt = _snc_packet([[3, 4, 0], [0, 0, 0]] + [[32767, 32767, 32767]] * 16)
    out = decode_samples("snc", pkt)
    # 18 rows (one per interleaved sample); each row has 3 channels + 3 rms.
    assert len(out) == 18
    assert out[0][:3] == [3.0, 4.0, 0.0]
    # rms1 over valid ch0 values {3, 0, ...clipped ignored} = sqrt((9+0)/2)
    import math
    rms1 = out[0][3]
    assert math.isclose(rms1, math.sqrt((9 + 0) / 2), rel_tol=1e-6)
    # Every row carries the same per-packet RMS triple.
    assert out[5][3:] == out[0][3:]


def test_csv_sink_header_and_imu_row(tmp_path):
    path = tmp_path / "out.csv"
    sink = CSVSink(str(path), flush_every=1)
    sink.open()
    sink.write("imu", 42, b"\x04\x02", [1, 2, 3])
    sink.close()

    lines = path.read_text().strip().splitlines()
    assert lines[0].split(",") == [
        "timestamp_ns", "timestamp_iso", "stream",
        "snc1", "snc2", "snc3", "rms1", "rms2", "rms3", "ax", "ay", "az",
    ]
    row = lines[1].split(",")
    assert row[0] == "42"
    assert row[2] == "imu"
    # No raw_hex column by default.
    assert row[3:9] == ["", "", "", "", "", ""]  # snc + rms empty
    assert row[9:12] == ["1", "2", "3"]          # ax, ay, az
    assert sink.count == 1


def test_csv_sink_snc_row(tmp_path):
    path = tmp_path / "snc.csv"
    sink = CSVSink(str(path), flush_every=1)
    sink.open()
    sink.write("snc", 7, b"\x2e\xff", [471.0, 12.0, -3.0, 100.0, 5.0, 2.0])
    sink.close()
    row = path.read_text().strip().splitlines()[1].split(",")
    assert row[2] == "snc"
    assert row[3:6] == ["471.0", "12.0", "-3.0"]   # snc1..3
    assert row[6:9] == ["100.0", "5.0", "2.0"]     # rms1..3
    assert row[9:] == ["", "", ""]                 # imu columns empty


def test_csv_sink_include_raw_bytes(tmp_path):
    path = tmp_path / "raw.csv"
    sink = CSVSink(str(path), flush_every=1, include_raw=True)
    sink.open()
    sink.write("imu", 1, b"\xab\xcd", [1, 2, 3])
    sink.close()
    lines = path.read_text().strip().splitlines()
    assert lines[0].split(",")[3] == "raw_hex"
    row = lines[1].split(",")
    assert row[3] == "abcd"
