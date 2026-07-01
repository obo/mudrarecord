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


def test_decode_snc_samples_uses_markers():
    # header (2 bytes) then [int16][00 80] records
    buf = bytearray([0x2E, 0xFF])
    for v in (471, 1520, -819):
        buf += struct.pack("<h", v) + protocol.SNC_MARKER
    out = protocol.decode_snc_samples(bytes(buf))
    assert out == [471, 1520, -819]


def test_decode_snc_samples_empty():
    assert protocol.decode_snc_samples(b"") == []
    assert protocol.decode_snc_samples(b"\x2e\xff") == []


def test_parse_battery_and_charging():
    assert protocol.parse_battery(b"\x55") == 0x55
    assert protocol.parse_battery(b"") == 0
    assert protocol.parse_charging(bytes([protocol.CHARGING_TRUE])) is True
    assert protocol.parse_charging(b"\x00") is False


def test_parse_serial_part_sides():
    assert protocol.parse_serial_part(b"1A", "right") == 0x1A
    assert protocol.parse_serial_part(b"1A", "left") == 0x1A * 1_000_000
    assert protocol.parse_serial_part(b"zz", "right") == 0
