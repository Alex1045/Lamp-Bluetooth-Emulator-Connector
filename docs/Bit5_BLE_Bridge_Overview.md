# How the Bit5 BLE bridge works

*High-level architecture | macOS, Python and Pixel_7*

The bridge lets BitDIY inside an Android emulator communicate with a physical Bit5 through the Mac. Python joins two separate BLE connections: a real radio connection to Bit5 and a simulated connection to Android.

## The pipeline

```text
BitDIY in Pixel_7
↕ Android Bluetooth
Netsim simulated Bluetooth controller
↕ HCI transport
Bumble virtual Bit5_L1
↕ Python forwards command and sensor bytes
Bleak → macOS CoreBluetooth → Mac Bluetooth radio
↕ Physical BLE connection
Real Bit5_L1 and its attached sensors
```

## What each component does

BitDIY is the Android app that discovers the virtual device, sends commands and displays sensor values. Pixel_7 provides the Android environment.

Netsim simulates the Bluetooth controller and radio. Bumble implements the virtual peripheral and its GATT database—the services and characteristics Android can discover, read, write or subscribe to.

`bit5_live.py` forwards communication between Bumble and Bleak. Bleak accesses the real device through macOS CoreBluetooth and the Mac’s built-in Bluetooth radio. No Swift or Xcode is required.

## Why a virtual device is needed

The emulator’s Bluetooth radio is simulated. This setup does not share the Mac’s physical Bluetooth connection with Android. Instead, Android talks to a virtual Bit5, and Python relays its bytes to the real one.

## Starting and identifying the devices

Run `./start_bit5.sh` from the project folder. It starts or reuses Pixel_7, waits for Netsim and launches the live bridge using the existing Python environment.

The bridge loads `profiles/bit-device.json`. Bleak selects the physical Bit5 by the exact CoreBluetooth identifier captured in that profile, rather than by name. Other nearby devices with the same name are not selected.

Bumble advertises Bit5_L1 with service FFE0, while exposing GATT services 180A and FFF0 and communication characteristic FFF1. This difference matches the real device. The current virtual address ends in F7; it is separate from the physical device’s macOS identifier.

## Commands travel toward Bit5

When BitDIY writes a command to virtual FFF1, Bumble receives it and Python forwards the same payload through Bleak to real FFF1, using a write without response. The bridge logs the bytes; BitDIY and the device define their meaning.

A submitted write confirms that the Bluetooth API accepted it for transmission, not that the device executed it. FFF1 reads return “Read Not Permitted,” matching the observed physical behavior.

## Sensor data travels toward Android

Bleak subscribes to real FFF1 notifications. When Bit5 sends data, Python forwards it through Bumble and Netsim to Android—but only after BitDIY enables notifications on virtual FFF1.

Large payloads are split into ordered chunks to fit Android’s BLE packet limit. The bytes are preserved, although notification boundaries may change. There are no invented sensor responses.

## Connection checkpoints and recovery

`REAL_CONNECTED` means the Mac reached the physical Bit5. `ANDROID_CONNECTION` means Android reached the virtual device. `FFF1_SUBSCRIPTION` with `notify=True` means BitDIY enabled sensor data. Both connections and that subscription are needed for the complete path.

The virtual device stays available while the physical connection retries. Startup writes wait briefly for the real device; expired writes are dropped. The bridge refreshes idle advertising and retries failed transport connections. Logs are saved in `logs/bit5-live.log`.

## Audio and network are separate

Microphone input, speech recognition and text-to-speech use the emulator’s audio and network systems. They do not pass through the BLE bridge. A speech-service failure can occur while Bluetooth communication is working.
