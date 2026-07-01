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


def test_decode_samples_imu_expands_per_sample():
    pkt = _imu_packet([(1, 2, 3), (4, 5, 6)], [0.5, 0.25])
    out = decode_samples("imu", pkt)
    assert out[0] == [1, 2, 3]
    assert out[1] == [4, 5, 6]


def test_decode_samples_snc_one_row_per_value():
    from mudrarecord import protocol

    buf = bytearray([0x2E, 0xFF])
    for v in (100, -200, 300):
        buf += struct.pack("<h", v) + protocol.SNC_MARKER
    out = decode_samples("snc", bytes(buf))
    assert out == [[100], [-200], [300]]


def test_csv_sink_header_and_imu_row(tmp_path):
    path = tmp_path / "out.csv"
    sink = CSVSink(str(path), flush_every=1)
    sink.open()
    sink.write("imu", 42, b"\x04\x02", [1, 2, 3])
    sink.close()

    lines = path.read_text().strip().splitlines()
    assert lines[0].split(",") == [
        "timestamp_ns", "timestamp_iso", "stream", "raw_hex",
        "snc", "ax", "ay", "az",
    ]
    row = lines[1].split(",")
    assert row[0] == "42"
    assert row[2] == "imu"
    assert row[3] == "0402"
    assert row[4] == ""          # snc column empty for imu row
    assert row[5:8] == ["1", "2", "3"]
    assert sink.count == 1


def test_csv_sink_snc_row_only_fills_snc_column(tmp_path):
    path = tmp_path / "snc.csv"
    sink = CSVSink(str(path), flush_every=1)
    sink.open()
    sink.write("snc", 7, b"\x2e\xff", [471])
    sink.close()
    row = path.read_text().strip().splitlines()[1].split(",")
    assert row[2] == "snc"
    assert row[4] == "471"        # snc column
    assert row[5:] == ["", "", ""]  # imu columns empty
