"""mudrarecord command-line interface.

Commands:
    mudrarecord scan                 Find nearby Mudra Band devices.
    mudrarecord info                 Connect and show device info + channel state.
    mudrarecord stream-csv           Record all raw channels to CSV (file or stdout).
    mudrarecord stream-lsl           Publish all raw channels as LSL streams.

Streaming disables the band's HID output first, so it does NOT act as a mouse
or keyboard while recording. Every emitted sample carries an exact global
nanosecond wall-clock timestamp captured at BLE-packet arrival.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from .recorder import CSVSink, Decimator, LSLSink, decode_samples


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mudrarecord",
        description="Mudra Band Recorder - record raw channels to CSV or LSL.",
    )
    parser.add_argument("--version", action="version", version=f"mudrarecord {__version__}")
    sub = parser.add_subparsers(dest="command")

    scan_p = sub.add_parser("scan", help="Scan for Mudra Band devices")
    scan_p.add_argument("--timeout", type=float, default=10.0, help="Scan timeout (s)")

    info_p = sub.add_parser("info", help="Show device info and channel state")
    info_p.add_argument("--address", help="Device address (skip discovery)")

    for name, help_text in [
        ("stream-csv", "Record raw channels to CSV"),
        ("stream-lsl", "Publish raw channels over LSL"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--address", help="Device address (skip discovery)")
        p.add_argument(
            "--rate",
            default="max",
            help="Output rate: 'max' (every packet, default) or an integer "
            "decimation factor N (keep 1 of every N samples).",
        )
        p.add_argument(
            "--sample-type",
            choices=["16bit", "24bit"],
            help="Device ADC sample type (default: leave device setting unchanged).",
        )
        p.add_argument("--skip-snc", action="store_true", help="Do not enable/record SNC (sEMG)")
        p.add_argument(
            "--skip-acc", action="store_true",
            help="Do not record accelerometer. Acc and gyro share one IMU "
            "packet, so the IMU stream is only disabled (saving BLE bandwidth) "
            "when BOTH --skip-acc and --skip-gyro are given.",
        )
        p.add_argument(
            "--skip-gyro", action="store_true",
            help="Do not record gyroscope (gyro is never decoded into columns; "
            "see --skip-acc note about the shared IMU packet).",
        )
        p.add_argument(
            "--restore-hid",
            action="store_true",
            help="Re-enable gesture->HID output on exit (default: leave HID off).",
        )
        if name == "stream-csv":
            p.add_argument(
                "-o", "--output", default="-",
                help="Output CSV path, or '-' for stdout (default).",
            )
            p.add_argument(
                "--include-raw-bytes",
                action="store_true",
                help="Add a raw_hex column with the verbatim BLE packet bytes "
                "(default: emit only decoded float values).",
            )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return

    try:
        asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def _dispatch(args) -> None:
    if args.command == "scan":
        await _cmd_scan(args)
    elif args.command == "info":
        await _cmd_info(args)
    elif args.command == "stream-csv":
        await _cmd_stream(args, mode="csv")
    elif args.command == "stream-lsl":
        await _cmd_stream(args, mode="lsl")


async def _get_device(address: str | None):
    from .ble import BleConnection
    from .device import MudraBand

    if address:
        found = await BleConnection.discover(timeout=5.0)
        match = next((d for d in found if d.address == address), None)
        if match:
            return MudraBand(match)
        return MudraBand.from_address(address)
    return await MudraBand.discover()


# --------------------------------------------------------------------------
# scan / info
# --------------------------------------------------------------------------

async def _cmd_scan(args) -> None:
    from .ble import BleConnection

    print(f"Scanning for {args.timeout:g}s...", file=sys.stderr)
    devices = await BleConnection.discover(args.timeout)
    if not devices:
        print("No Mudra Band devices found.")
        return
    for d in devices:
        print(f"  {(d.name or 'Unknown'):20s}  {d.address}")
    print(f"\n{len(devices)} device(s) found.")


async def _cmd_info(args) -> None:
    device = await _get_device(args.address)
    await device.connect()
    await device.refresh_status()
    await asyncio.sleep(0.3)

    s = device.status
    print(f"Name:              {device.name}")
    print(f"Address:           {device.address}")
    print(f"Firmware:          {device.firmware_version}")
    print(f"Serial:            {device.serial_number}")
    print(f"Battery:           {device.battery_level}%")
    print(f"Charging:          {device.is_charging}")
    print(f"Hand:              {s.hand}")
    print(f"Band mode:         {s.band_mode}")
    print(f"SNC enabled:       {s.is_snc_enabled}")
    print(f"Acc enabled:       {s.is_acc_enabled}")
    print(f"Gyro enabled:      {s.is_gyro_enabled}")
    print(f"Gesture enabled:   {s.is_gesture_enabled}")
    print(f"Gesture -> HID:    {s.is_gesture_to_hid_enabled}")
    print(f"Nav -> HID:        {s.is_nav_to_hid_enabled}")

    await device.disconnect()


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

def _parse_rate(rate: str) -> int:
    if rate == "max":
        return 1
    try:
        factor = int(rate)
    except ValueError:
        raise SystemExit(f"--rate must be 'max' or an integer, got {rate!r}")
    if factor < 1:
        raise SystemExit("--rate factor must be >= 1")
    return factor


async def _cmd_stream(args, mode: str) -> None:
    factor = _parse_rate(args.rate)

    record_snc = not args.skip_snc
    record_acc = not args.skip_acc
    record_gyro = not args.skip_gyro
    record_imu = record_acc or record_gyro

    if not record_snc and not record_imu:
        raise SystemExit("Nothing to record: all channels skipped.")

    device = await _get_device(args.address)
    await device.connect()

    # Stop the band from acting as mouse/keyboard.
    await device.disable_hid()

    if args.sample_type:
        await device.set_sample_type(args.sample_type)

    # Enable only the requested channels. Skipping channels reduces the BLE
    # bandwidth the firmware spends, which can raise the effective rate of the
    # channels that remain.
    #
    # Order matters: enable the IMU stream BEFORE SNC. If SNC is enabled first
    # it immediately floods the BLE link, and the subsequent IMU enable/subscribe
    # is frequently lost so the accelerometer never starts (the "acc fully
    # missing" symptom). Bringing IMU up first lets both streams establish.
    if record_imu:
        if record_acc:
            await device.enable_imu_acc()
            await asyncio.sleep(0.1)
        if record_gyro:
            await device.enable_imu_gyro()
            await asyncio.sleep(0.1)
        await device.start_imu_notify()
        await asyncio.sleep(0.1)
    if record_snc:
        await device.enable_snc()
        await asyncio.sleep(0.1)
        await device.start_snc_notify()

    await asyncio.sleep(0.2)
    await device.refresh_status()
    await asyncio.sleep(0.3)

    # Build the sink.
    if mode == "csv":
        sink = CSVSink(args.output, include_raw=args.include_raw_bytes)
    else:
        sink = LSLSink(source_id=str(device.serial_number or device.address))
    sink.open()

    decimator = Decimator(factor)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    disk_full = {"flag": False}
    # Monotonic time of the last packet seen per stream (for the watchdog).
    last_seen = {"snc": 0.0, "imu": 0.0}

    def emit(stream: str, ts_ns: int, raw: bytes) -> None:
        last_seen[stream] = loop.time()
        if not decimator.accept(stream):
            return
        try:
            for channels in decode_samples(stream, raw):
                sink.write(stream, ts_ns, raw, channels)
        except OSError as e:
            # e.g. ENOSPC (disk full). Stop cleanly.
            disk_full["flag"] = True
            print(f"\nWrite failed ({e}); stopping.", file=sys.stderr)
            loop.call_soon_threadsafe(stop_event.set)

    if record_snc:
        device.on_raw_snc(lambda ts, data: emit("snc", ts, data))
    if record_imu:
        device.on_raw_imu(lambda ts, data: emit("imu", ts, data))

    @device.on_disconnect
    def _on_disc() -> None:
        loop.call_soon_threadsafe(stop_event.set)

    # Report to stderr so stdout can carry CSV.
    s = device.status
    enabled = []
    if record_snc:
        enabled.append(f"SNC({'on' if s.is_snc_enabled else 'req'})")
    if record_acc:
        enabled.append(f"ACC({'on' if s.is_acc_enabled else 'req'})")
    if record_gyro:
        enabled.append(f"GYRO({'on' if s.is_gyro_enabled else 'req'})")
    print(
        f"Recording from {device.name} [{', '.join(enabled)}] "
        f"HID off, rate={'max' if factor == 1 else f'1/{factor}'}. "
        f"Ctrl+C to stop.",
        file=sys.stderr,
    )
    if s.is_gesture_to_hid_enabled or s.is_nav_to_hid_enabled:
        print("Warning: device still reports HID routing enabled.", file=sys.stderr)

    # Watchdog: the band occasionally drops a stream (a lost enable command
    # while it is waking up, or a stall after the LED-sleep cycle), which shows
    # up as a channel that is "fully missing". If a requested stream produces no
    # packets for WATCHDOG_TIMEOUT seconds, re-send its enable command and
    # re-subscribe so recording recovers on its own.
    WATCHDOG_TIMEOUT = 2.0
    now0 = loop.time()
    for st in last_seen:
        last_seen[st] = now0

    async def watchdog() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(1.0)
            if not device.is_connected:
                continue
            now = loop.time()
            try:
                if record_snc and now - last_seen["snc"] > WATCHDOG_TIMEOUT:
                    print("Watchdog: SNC stream silent, re-enabling.", file=sys.stderr)
                    await device.enable_snc()
                    await device.start_snc_notify()
                    last_seen["snc"] = now
                if record_imu and now - last_seen["imu"] > WATCHDOG_TIMEOUT:
                    print("Watchdog: IMU stream silent, re-enabling.", file=sys.stderr)
                    if record_acc:
                        await device.enable_imu_acc()
                    if record_gyro:
                        await device.enable_imu_gyro()
                    await device.start_imu_notify()
                    last_seen["imu"] = now
            except Exception as e:
                print(f"Watchdog re-enable failed: {e}", file=sys.stderr)

    wd_task = loop.create_task(watchdog())

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        wd_task.cancel()
        await _shutdown(device, sink, args, record_snc, record_acc, record_gyro)


async def _shutdown(device, sink, args, record_snc, record_acc, record_gyro) -> None:
    try:
        if device.is_connected:
            if record_snc:
                await device.disable_snc()
            if record_acc:
                await device.disable_imu_acc()
            if record_gyro:
                await device.disable_imu_gyro()
            if getattr(args, "restore_hid", False):
                await device.enable_hid()
    except Exception:
        pass
    finally:
        sink.close()
        try:
            await device.disconnect()
        except Exception:
            pass
        print(f"\n{sink.count} record(s) written.", file=sys.stderr)


if __name__ == "__main__":
    main()
