# mudrarecord — Mudra Band Recorder

Fast, lightweight recording of the **Mudra Band**'s *raw* channels to **CSV** or
**LSL** (Lab Streaming Layer), with exact global **nanosecond** timestamps.

`mudrarecord` is a small, fully self-contained tool. It talks to the band directly
over BLE and does one thing well: stream every raw channel out as fast as it
arrives, for as long as you like (until the disk fills up), while the band is
prevented from acting as a mouse or keyboard.

## Features

- Single CLI tool with four commands: `scan`, `info`, `stream-csv`, `stream-lsl`.
- Records the raw channels the band provides: **SNC** (sEMG) and the **IMU**
  packet (decoded **accelerometer**; raw **gyroscope** bytes preserved).
- Exact global timestamps to the nanosecond (`time.time_ns()` captured at BLE
  packet arrival), in both CSV and LSL.
- HID is turned **off** during recording: the band does not send mouse/keyboard
  input while you record.
- Streams data out immediately as it arrives; nothing is buffered in memory, so
  it can run continuously for hours.
- LSL output is consumable by **MNE-Python**, **OpenViBE**, and other LSL tools.
- Per-channel `--skip-*` flags: skipped channels are never enabled on the
  device, so the band spends no bandwidth on them and the remaining channels can
  in principle reach a higher rate.

## Requirements

- Python 3.10+
- Linux with BlueZ (BLE via `bleak`)
- A Mudra Band, paired via `bluetoothctl`
- For LSL output: `pylsl` (installs the LSL runtime) — see below

## Installation

```bash
pip install .
```

For development (editable install with test dependencies):

```bash
pip install -e ".[dev]"
```

To include LSL output support (`stream-lsl`):

```bash
pip install -e ".[dev,lsl]"
```

## Connecting to the band

Getting a reliable BLE link to the Mudra Band is the most common source of
trouble. There are **two separate layers** and it helps to diagnose them
independently:

1. **OS / BlueZ layer** — the operating system must have an active BLE
   connection to the band (`bluetoothctl` reports `Connected: yes`).
2. **mudrarecord layer** — `mudrarecord` (via `bleak`) opens a GATT client on top of
   that OS connection, discovers services, and subscribes to notifications.

If layer 1 is not up, layer 2 cannot work. Always confirm the OS-level
connection first.

### The band goes to sleep

The band's LED blinks orange for about 30–60 seconds after you press its
button, then the LED goes dark and the band **stops advertising / accepting
connections**. To connect, press the button so it blinks, and start the
connection *while it is blinking*. If it stops blinking mid-attempt, press it
again and retry. Keeping the band awake during the whole connect sequence is
often all that is needed.

### Step 1 — check the OS-level connection

```bash
bluetoothctl devices | grep -i mudra        # list known bands + addresses
bluetoothctl info <ADDRESS> | grep -Ei 'Name|Paired|Trusted|Connected'
```

- `Connected: yes` → the OS link is up; go to Step 2.
- `Connected: no` → bring the link up (band blinking):

  ```bash
  bluetoothctl connect <ADDRESS>
  ```

  Note: the **first** `connect` call starts a background attempt and returns
  immediately; subsequent calls may print
  `org.bluez.Error.Failed: Operation already in progress` while that attempt is
  still running. This is normal — wait a few seconds and re-check
  `bluetoothctl info <ADDRESS>` until it shows `Connected: yes`. A short retry
  loop works well:

  ```bash
  for i in $(seq 1 6); do
    bluetoothctl connect <ADDRESS>
    sleep 3
    bluetoothctl info <ADDRESS> | grep -q 'Connected: yes' && break
  done
  ```

Marking the band **trusted** helps BlueZ auto-reconnect and reduces drops:

```bash
bluetoothctl trust <ADDRESS>
```

### Step 2 — check the mudrarecord-level connection

With the OS link up, confirm mudrarecord can open the GATT layer:

```bash
mudrarecord scan          # should list the band (discovery works independently)
mudrarecord info          # connects, reads firmware/battery/channel state
```

- `mudrarecord info` prints device details → the full stack works; you can stream.
- `mudrarecord info` fails with `Failed to connect ... Operation already in
  progress` → a BlueZ connect attempt is still pending or stuck. Wait, or reset
  it (see below).
- `mudrarecord info` fails with an *empty* error message → BlueZ dropped/rejected
  the connection (band asleep or out of range). Wake the band and retry Step 1.

Tip: pass `--address <ADDRESS>` to `info` / `stream-*` to skip discovery and go
straight to a known band — useful when several Mudra devices are nearby.

### Fresh start: unpair and re-pair

A stale bond (for example, an old address that no longer advertises, or a
half-broken pairing) is a frequent cause of connections that never complete.
The cleanest fix is to remove the pairing and pair again from scratch.

```bash
# 1) Remove the existing bond (unpair). This also disconnects it.
bluetoothctl remove <ADDRESS>

# 2) Put the band into pairing mode: press its button so the LED blinks orange.

# 3) Pair + trust + connect again automatically, while it blinks:
bluetoothctl --timeout 30 scan on            # find the band; note its ADDRESS
bluetoothctl pair <ADDRESS>
bluetoothctl trust <ADDRESS>
bluetoothctl connect <ADDRESS>
```

Or as a one-liner scripted "fresh start" (replace `<ADDRESS>`), keeping the band
blinking throughout:

```bash
ADDR=<ADDRESS>
bluetoothctl remove "$ADDR" 2>/dev/null; sleep 2
bluetoothctl --timeout 15 scan on >/dev/null
bluetoothctl pair "$ADDR"; bluetoothctl trust "$ADDR"
for i in $(seq 1 8); do
  bluetoothctl connect "$ADDR"; sleep 3
  bluetoothctl info "$ADDR" | grep -q 'Connected: yes' && break
done
bluetoothctl info "$ADDR" | grep -Ei 'Paired|Trusted|Connected'
```

After a successful re-pair, `mudrarecord scan` will show the band and
`mudrarecord info` will connect. Note that a re-pair can change the band's BLE
address; re-run `mudrarecord scan` (or `bluetoothctl devices`) to get the current
one.

## Usage

```bash
mudrarecord scan                         # find nearby bands
mudrarecord info                         # connect and print device + channel state

# Record everything (SNC + acc + gyro) to a CSV file at the device's max rate:
mudrarecord stream-csv -o recording.csv

# Stream to stdout (pipe it somewhere):
mudrarecord stream-csv -o - | your_consumer

# Publish over LSL for MNE-Python / OpenViBE:
mudrarecord stream-lsl

# Record only SNC, skipping the IMU entirely:
mudrarecord stream-csv --skip-acc --skip-gyro -o emg.csv

# Keep 1 of every 4 samples (host-side decimation):
mudrarecord stream-csv --rate 4 -o decimated.csv

# Choose the ADC sample type:
mudrarecord stream-csv --sample-type 24bit -o hires.csv
```

Recording stops on `Ctrl+C`, on device disconnect, or when the output disk is
full. By default HID stays off after exit; pass `--restore-hid` to turn
gesture→HID back on when you stop.

### Options common to both stream commands

| Option           | Meaning                                                              |
|------------------|----------------------------------------------------------------------|
| `--address ADDR` | Connect to a specific BLE address, skipping discovery.               |
| `--rate max\|N`  | `max` (default) emits every packet; `N` keeps 1 of every N samples.  |
| `--sample-type`  | `16bit` or `24bit` ADC sample type (default: leave device unchanged).|
| `--skip-snc`     | Do not enable/record the SNC (sEMG) stream.                          |
| `--skip-acc`     | Do not record the accelerometer (see IMU packet note below).        |
| `--skip-gyro`    | Do not record the gyroscope (see IMU packet note below).            |
| `--restore-hid`  | Re-enable gesture→HID output on exit.                               |

`stream-csv` additionally has `-o/--output` (file path or `-` for stdout).

Accelerometer and gyroscope are delivered together in a single IMU packet, so
the IMU stream is only disabled — freeing BLE bandwidth for the remaining
channels — when **both** `--skip-acc` and `--skip-gyro` are given.

## Output formats

### CSV

One row per decoded sample. Columns:

```
timestamp_ns, timestamp_iso, stream, raw_hex, snc, ax, ay, az
```

- `timestamp_ns` — global wall-clock time in nanoseconds since the Unix epoch,
  captured when the BLE packet arrived.
- `timestamp_iso` — the same instant as ISO-8601 UTC with nanosecond precision.
- `stream` — `snc` or `imu`.
- `raw_hex` — the raw BLE payload, verbatim (authoritative; also carries the
  raw gyroscope bytes, which are not decoded into columns).
- `snc` — decoded sEMG value (filled only on `snc` rows).
- `ax, ay, az` — decoded int16 accelerometer axes (filled only on `imu` rows).

Columns not relevant to a given row's `stream` are left empty. A single IMU BLE
packet contains several accelerometer samples; each becomes its own row (sharing
that packet's timestamp and `raw_hex`). Likewise each SNC value in a packet
becomes its own row.

Diagnostic/status messages are written to **stderr**, so `stdout` carries clean
CSV when you use `-o -`.

### LSL

Up to two outlets are created as data arrives:

- `Mudra-SNC` — type `EMG`, one `float32` channel labelled `snc`.
- `Mudra-IMU` — type `Motion`, three `float32` channels labelled `ax, ay, az`
  (accelerometer). Gyroscope is not published over LSL (use CSV `raw_hex`).

Each sample is pushed with its exact wall-clock timestamp (converted to LSL
seconds), and channel labels are written into the stream metadata so MNE-Python
and OpenViBE display named channels. The nominal sampling rate is advertised as
*irregular* because the band streams at its own device-driven rate.

Example: resolve the streams in MNE-Python via `mne_lsl` / `pylsl`, or add an
"LSL Acquisition" box in OpenViBE and select the `Mudra-SNC` / `Mudra-IMU`
stream.

## Important caveats

These reflect what has been reverse-engineered from the band's raw BLE streams
(observed on firmware 6.0.11.5). They are documented here so the recorded data
is interpreted correctly. In all cases the verbatim `raw_hex` column is the
authoritative record.

- **Accelerometer is verified.** The IMU packet (type byte `0x04`) carries 8
  accelerometer samples as int16 little-endian `(ax, ay, az)` triplets; these
  decode cleanly and reproducibly and are emitted in the `ax/ay/az` columns.

- **Gyroscope is not decoded.** The same IMU packet also carries a gyroscope
  block whose byte layout is not reliably understood, so `mudrarecord` does not
  turn it into columns. Its raw bytes are preserved in the IMU `raw_hex` if you
  want to decode it yourself. Because acc and gyro share one packet, they cannot
  be recorded independently; the IMU stream is only fully disabled when both
  `--skip-acc` and `--skip-gyro` are set.

- **SNC decoding is experimental.** The sEMG samples are extracted as the int16
  value preceding each `00 80` marker in the SNC packet (type byte `0x2e`),
  which yields a clean oscillating waveform. However the exact channel count and
  scaling are not officially documented, so treat the `snc` column as
  experimental and rely on `raw_hex` for anything authoritative.

- **No firmware "sample rate" knob.** The device pushes BLE notifications at its
  own native (maximum) rate; there is no documented command to set a target Hz.
  `--rate max` simply emits every packet as it arrives. A numeric `--rate N`
  performs host-side decimation on the output. The one real device-side knob is
  `--sample-type` (16-bit vs 24-bit ADC resolution). Skipping channels you do
  not need (`--skip-*`) frees BLE bandwidth, which can in principle raise the
  effective rate of the remaining channels.

- **Timestamps are host-side.** Global timestamps are taken on the recording
  host at BLE-packet arrival. They therefore include BLE transport/stack
  latency and jitter, but provide a consistent global time base across all
  channels. Samples expanded from the same BLE packet share that packet's
  arrival timestamp.

## Architecture

```
mudrarecord/
  protocol.py   - BLE UUIDs, status indices, raw IMU/SNC decoders
  commands.py   - firmware command byte builders (channels, HID, sample type)
  ble.py        - BLE connection; timestamps notifications at arrival
  status.py     - firmware status parser (info + verification)
  device.py     - MudraBand: connect, info, HID off, channel enable, raw callbacks
  recorder.py   - CSVSink, LSLSink, decimation, raw decoders
  cli.py        - scan / info / stream-csv / stream-lsl
```

## Testing

```bash
pytest tests/ -v
```

The tests cover the pure-Python decode, timestamp formatting, decimation, and
CSV writing; no hardware is required.

## License

MIT
