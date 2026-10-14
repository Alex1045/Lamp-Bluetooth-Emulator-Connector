# Lamp Bluetooth Emulator Connector

Python-only bridge for macOS 14.7.4. Bleak uses the Mac's built-in Bluetooth to connect to the real Bit4/Bit5. Google Bumble hosts a second, virtual BLE peripheral on Android Emulator Netsim for Android apps to discover and connect to.

## Important Boundary

The emulator radio is virtual. The Mac and emulator are not sharing a physical BLE connection: Bleak connects to the real device, and Bumble advertises a GATT mirror into Netsim. This requires an Android Emulator version with Bluetooth/Netsim support (Bumble currently documents Emulator 33.1.4.0 or later). It does not install or use Xcode or Swift.

macOS CoreBluetooth gives Bleak decoded advertising fields and an opaque per-device identifier, not a controllable copy of the peripheral's over-the-air address or guaranteed raw advertising bytes. The bridge preserves the advertised name and discovered service/characteristic UUIDs and properties, and mirrors known service UUIDs and transmit power where possible. Exact address, raw payload, security/bond state, timing, and controller-specific behavior cannot be cloned by this path. Advertising payload limits may also require dropping fields.

## Setup

### One-command live Bit5 launcher

```sh
./start_bit5.sh
```

The launcher starts or reuses Pixel_7, waits for Android and Netsim, and runs
`bit5_live.py` in the existing virtual environment. Power on the real Bit5 and
wait for `REAL_CONNECTED` before connecting in BitDIY. Ctrl-C stops the bridge
and leaves the emulator open. BLE traffic is logged to `logs/bit5-live.log`;
emulator startup output is in `logs/pixel7.log`. Only one Bit5 peripheral process
can run at a time. The live bridge keeps the virtual device available while
retrying a lost physical connection. Startup writes wait up to ten seconds for
the real device; expired writes and writes from a disconnected Android client
are dropped. Physical scanning stops as soon as the captured identifier appears.
Advertising uses a 100–150 ms interval and is refreshed every 30 seconds while
no Android client is connected. Transport failures restart with a 2–30 second
backoff. `BRIDGE_STATUS` reports both connections and notification subscriptions
every 15 seconds. These recovery measures do not guarantee BitDIY compatibility.

Use the existing Python 3.13 installation or another Python 3.11+ environment. The Android optional dependencies for Bumble include gRPC/protobuf; they are required for Netsim transport.

```sh
cd Lamp-Bluetooth-Emulator-Connector
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Grant Bluetooth permission to the terminal/VS Code process in macOS System Settings if prompted. Android Studio must already have the Android Emulator and the `Pixel_7` AVD installed. No Xcode install is needed.

## Stage 1: Scan

```sh
python ble_bridge.py --verbose scan --timeout 12 --filter Bit
```

The scan logs local name, CoreBluetooth identifier, RSSI, service UUIDs, manufacturer data, and service data. If the peripheral advertises an unexpected name, omit `--filter` and inspect the full list.

## Stage 2: GATT Access

For the separate Bleak-only diagnostic (without starting Bumble or the proxy):

```sh
.venv/bin/python inspect_bit_device.py --output profiles/bit-device.json
```

This scans every nearby device for at least 15 seconds, prompts you to select a
discovered device, enumerates GATT, reads readable characteristics, and subscribes
to notify/indicate characteristics for at least 30 seconds. Interact with the
physical device when prompted. Advertisement callbacks, GATT details, read
results, subscription failures, and notification payloads are saved as JSON.
Identifiers remain the UUIDs supplied by CoreBluetooth. Unknown service primary
status is recorded as `null`. Notification subscriptions involve the normal BLE
subscription operation; the script does not send application characteristic writes.

To save only advertisements without connecting:

```sh
.venv/bin/python inspect_bit_device.py --scan-only --output profiles/bit-device-scan.json
```

```sh
python ble_bridge.py --verbose inspect --device Bit4 --observe-seconds 8
```

Use `--device Bit5` or pass part/all of the identifier printed by `scan`. Inspection connects, enumerates services/characteristics/descriptors, reads readable values, attempts descriptor reads, subscribes to notify/indicate characteristics for the observation window, logs updates, disconnects, and saves a JSON snapshot under `profiles/`. Failed or permission-protected reads are logged and do not stop discovery. Inspection does not write to the peripheral.

## Stage 3: Bumble/Netsim Smoke Test

In one terminal, start the AVD with Netsim Bluetooth enabled:

```sh
./launch_pixel7.sh
```

The helper resolves `ANDROID_SDK_ROOT`, `ANDROID_HOME`, or the standard macOS SDK path, verifies that `Pixel_7` exists, and runs the emulator with `-packet-streamer-endpoint default`. The emulator starts/uses Netsim. In another terminal with the virtual environment active:

```sh
python ble_bridge.py --verbose bumble-smoke --transport android-netsim
```

The smoke test advertises a synthetic `Bumble BLE smoke` device with service `FFF0` and readable characteristic `FFF1`. Check Android Bluetooth scanning before proceeding. Netsim's gRPC port can be specified if automatic discovery fails, for example `--transport android-netsim:localhost:8555` (use the port reported by the local emulator/Netsim instance).

## Stage 4: Full Proxy

Leave the AVD running, stop the smoke test with Ctrl-C, then run:

```sh
python ble_bridge.py --verbose proxy --device Bit4 --transport android-netsim
```

The process connects to the real peripheral first, mirrors its service and characteristic database, then advertises the mirror to Android. Reads and writes are forwarded through Bleak. Non-CCCD descriptors are represented with read/write forwarding; Bumble manages the Android-side CCCD locally and starts/stops the corresponding physical notification subscription as Android subscribes/unsubscribes. Notification values received by Bleak are passed to subscribed Android clients. Logs identify each side and include characteristic UUIDs, handles, and hex values. `--device Bit5` selects Bit5.

If selection is ambiguous, scan first and use the unique name fragment or identifier. If BitDIY still cannot discover/connect, capture logs from both `proxy` and the Android app, verify the app has scan/connect permissions enabled, and confirm that Android can see the Stage 3 smoke peripheral. `--verbose` enables Bleak/Bumble debug logs.

## Useful Android Commands

If Android SDK platform tools are installed:

```sh
"${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}/platform-tools/adb" devices
"${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}/platform-tools/adb" -s emulator-5554 logcat -c
"${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}/platform-tools/adb" -s emulator-5554 logcat -v time
```

The `proxy` command is the Bumble Netsim launcher for the full mirror. The separate `bumble-smoke` command validates Bumble and Netsim without requiring the physical Bit device.


## Capture your physical device before launching

Device profiles are local and excluded from Git. After setup, capture your Bit5:

```sh
.venv/bin/python inspect_bit_device.py --output profiles/bit-device.json
```

Select the intended device and let inspection complete, then run `./start_bit5.sh`.
For a second Bit5, capture `profiles/bit5-second.json`, open Pixel_7 with
`./launch_pixel7.sh`, and run `./start_bit5_second.sh` in a second terminal.
Stop the previous bridge first.

The live virtual device uses address `F0:F1:F2:F3:F4:F7`, advertises `FFE0`,
and exposes `FFF0` / `FFF1`. Select this device in BitDIY. The launcher keeps
audio enabled, uses host graphics, disables Vulkan and snapshots, and uses
two CPU cores. Microphone and speech services are separate from BLE.

For the high-level explanation, read [the pipeline overview](docs/Bit5_BLE_Bridge_Overview.md).
The APK is not distributed in this repository; install BitDIY separately.
