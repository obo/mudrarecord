import struct

from mudrarecord import protocol


def _imu_packet(acc_samples, gyro_floats):
    """Build a synthetic type-0x04 IMU packet from acc triplets and gyro floats."""
    buf = bytearray([protocol.IMU_TYPE_TAG])
    for ax, ay, az in acc_samples:
        buf += struct.pack("<3h", ax, ay, az)
    # pad accelerometer block up to the gyro offset
    while len(buf) < protocol.IMU_GYRO_OFFSET:
        buf.append(0)
    for i, g in enumerate(gyro_floats):
        buf += struct.pack("<h", i)  # counter prefix
        buf += struct.pack("<f", g)
    return bytes(buf)


def test_decode_acc_samples():
    acc = [(-828, -150, 78), (-718, -190, 93)]
    pkt = _imu_packet(acc, [0.0, 0.0])
    out = protocol.decode_acc_samples(pkt)
    assert out[:2] == [(-828, -150, 78), (-718, -190, 93)]


def test_decode_acc_wrong_type_returns_empty():
    pkt = b"\x2e" + b"\x00" * 60
    assert protocol.decode_acc_samples(pkt) == []


def test_decode_gyro_floats():
    pkt = _imu_packet([(0, 0, 0)] * 8, [0.00374, 0.0027])
    gyro = protocol.decode_gyro_floats(pkt)
    assert abs(gyro[0] - 0.00374) < 1e-6
    assert abs(gyro[1] - 0.0027) < 1e-6


def test_decode_imu_packet_returns_acc_triplets():
    acc = [(10, 20, 30), (40, 50, 60)]
    pkt = _imu_packet(acc, [1.5, 2.5])
    rows = protocol.decode_imu_packet(pkt)
    assert rows[0] == (10, 20, 30)
    assert rows[1] == (40, 50, 60)


def test_decode_imu_short_packet_is_empty():
    assert protocol.decode_imu_packet(b"\x04\x02\x03") == []


def _snc_packet(rows):
    """Build a 112-byte SNC packet from [c1,c2,c3] int rows (padded to 18)."""
    vals = []
    for r in rows:
        vals += list(r)
    while len(vals) < protocol.SNC_DATA_INT16:
        vals.append(protocol.SNC_CLIP_MAX)
    vals += [0, 0]  # footer
    return struct.pack("<%dh" % len(vals), *vals)


def test_decode_snc_channels_deinterleaves():
    rows_in = [[10, 20, 30], [11, 21, 31], [12, 22, 32]]
    pkt = _snc_packet(rows_in)
    out = protocol.decode_snc_channels(pkt)
    assert len(out) == 18
    assert out[0] == [10.0, 20.0, 30.0]
    assert out[1] == [11.0, 21.0, 31.0]
    assert out[2] == [12.0, 22.0, 32.0]


def test_decode_snc_channels_clip_is_nan():
    import math

    pkt = _snc_packet([[protocol.SNC_CLIP_MIN, 5, protocol.SNC_CLIP_MAX]])
    out = protocol.decode_snc_channels(pkt)
    assert math.isnan(out[0][0])
    assert out[0][1] == 5.0
    assert math.isnan(out[0][2])


def test_snc_packet_rms():
    import math

    # ch0 valid values 3 and 0 (rest clipped); rms = sqrt((9+0)/2)
    pkt = _snc_packet([[3, 100, 0], [0, 100, 0]])
    rms = protocol.snc_packet_rms(pkt)
    assert math.isclose(rms[0], math.sqrt((9 + 0) / 2), rel_tol=1e-6)
    assert rms[1] == 100.0  # only valid ch1 samples are 100, 100
    assert rms[2] == 0.0


def test_decode_snc_channels_short_packet_empty():
    assert protocol.decode_snc_channels(b"") == []
    assert protocol.decode_snc_channels(b"\x00" * 10) == []


def test_parse_battery_and_charging():
    assert protocol.parse_battery(b"\x55") == 0x55
    assert protocol.parse_battery(b"") == 0
    assert protocol.parse_charging(bytes([protocol.CHARGING_TRUE])) is True
    assert protocol.parse_charging(b"\x00") is False


def test_parse_serial_part_sides():
    assert protocol.parse_serial_part(b"1A", "right") == 0x1A
    assert protocol.parse_serial_part(b"1A", "left") == 0x1A * 1_000_000
    assert protocol.parse_serial_part(b"zz", "right") == 0
