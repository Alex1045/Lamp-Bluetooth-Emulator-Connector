# Run BitDIY with a physical Bit5 on macOS

This guide starts from a fresh clone of [Lamp-Bluetooth-Emulator-Connector](https://github.com/Alex1045/Lamp-Bluetooth-Emulator-Connector). It explains how to run BitDIY inside an Android emulator and connect it to your own physical Bit5 through your Mac.

```text
BitDIY in Pixel_7 ↔ Virtual Bit5 ↔ Python bridge ↔ Mac Bluetooth ↔ Real Bit5
```

Your real hardware still performs the sensing and LED control. The virtual device provides a route between the Android app and that hardware.

## 1. Install the prerequisites

You need:

- A Mac with Bluetooth enabled. This project was developed on macOS 14.7.4 with Apple Silicon; other platforms are not validated by this guide.
- Python **3.11 or newer** and Git available in your terminal.
- Android Studio, with **Android Emulator** and **Android SDK Platform-Tools** installed through its SDK Manager.
- A physical Bit5 and any sensors or LEDs you want to use.
- A BitDIY APK obtained separately from a trusted source. The repository does not include the Android app.

The bridge uses Python, Bleak and Bumble. You do not need to build Swift code or install full Xcode for the bridge.

Verify your terminal tools:

```sh
python3 --version
git --version
```

If `python3` is older than 3.11, install a supported Python version before continuing.

## 2. Clone the repository and install Python dependencies

```sh
git clone https://github.com/Alex1045/Lamp-Bluetooth-Emulator-Connector.git
cd Lamp-Bluetooth-Emulator-Connector
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

Run the remaining commands from this repository folder. You do not need to activate the environment: the commands and launchers use `.venv` directly.

Open the cloned folder in VS Code if desired. Each new terminal used below should be in that same folder.

## 3. Create the Pixel_7 Android emulator

In Android Studio, open **Device Manager**, create a virtual device using the **Pixel 7** hardware profile, and set its AVD name to exactly **`Pixel_7`**. The launch scripts require this name.

The original setup used an Android 13 / API 33 Google Play `arm64-v8a` image on Apple Silicon. Use a system image appropriate for your Mac's CPU. Other image combinations may need additional testing.

For the Android Studio workflow, see [Create and manage virtual devices](https://developer.android.com/studio/run/managing-avds.html).

The scripts look for the Android SDK at `~/Library/Android/sdk` by default. If your SDK is elsewhere, set its actual location in each terminal used for this project:

```sh
# Replace this example with the SDK location shown in Android Studio.
export ANDROID_SDK_ROOT="/path/to/your/Android/sdk"
```

Set up a short ADB command for your current terminal:

```sh
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
export PATH="$ANDROID_SDK_ROOT/platform-tools:$PATH"
adb version
"$ANDROID_SDK_ROOT/emulator/emulator" -list-avds
```

Confirm that `Pixel_7` appears. The `export` commands affect this terminal only; repeat them in another terminal if you want to use `adb` there.

## 4. Start Android and install BitDIY

Start Pixel_7 with the project's Netsim-enabled helper:

```sh
./launch_pixel7.sh
```

Keep that terminal open if the emulator runs in it. Allow Android to finish booting. Start the emulator through this helper for initial setup; an instance started elsewhere may not have the expected Netsim configuration.

In a second terminal, enter the repository folder, set up the ADB path as in step 3, and run:

```sh
adb devices
```

These examples assume Pixel_7 is `emulator-5554`. If another emulator occupies that serial, find the correct one in `adb devices` and substitute it in subsequent commands. You can check an emulator's AVD name with:

```sh
adb -s emulator-5554 emu avd name
```

Install your separately obtained APK, replacing the example path:

```sh
adb -s emulator-5554 install "/path/to/BitDIY.apk"
```

You can also drag an APK onto the emulator window, as described in Android's [app installation guide](https://developer.android.com/studio/run/emulator-install-add-files).

Enable Bluetooth inside Android. Open BitDIY and grant requested Nearby devices/Bluetooth and location permissions needed for discovery. Microphone permission is needed only for features that use it.

## 5. Discover and capture your physical Bit5

Power on the real Bit5 and disconnect it from other phones or apps. If several Bit5 devices have the same name, turn off the others during this step.

With any existing bridge stopped, run:

```sh
.venv/bin/python inspect_bit_device.py --output profiles/bit-device.json
```

The script scans for at least 15 seconds and asks you to select a device number. Select your Bit5. It then inspects the device and listens for notifications for at least 30 seconds. Interact with the sensors when prompted and let the script finish.

If macOS requests Bluetooth access, allow it for the terminal application hosting Python, such as Terminal or VS Code. If previously denied, check **System Settings → Privacy & Security → Bluetooth**.

**Every user must capture their own profile.** Profiles are excluded from Git, and macOS device identifiers are specific to the Mac that discovered them. A fresh clone does not contain a ready-to-use `profiles/bit-device.json`.

Running the command again replaces that profile. Check that the inspection connected successfully and completed before starting the bridge; a scan-only or failed capture is not a usable full profile.

To scan without connecting, use a separate output file:

```sh
.venv/bin/python inspect_bit_device.py --scan-only --output profiles/bit5-scan.json
```

## 6. Start the bridge and connect BitDIY

```sh
./start_bit5.sh
```

This starts or reuses Pixel_7, waits for Android and Netsim, then starts the live bridge using `profiles/bit-device.json`. For later sessions, this is the main startup command.

Keep the terminal open. Wait for **`REAL_CONNECTED`**, which means the Mac reached your physical Bit5.

Inside BitDIY, open its connection screen, refresh the device list and select the virtual device whose address ends in **F7** (`F0:F1:F2:F3:F4:F7`). Its name comes from your capture, for example `Bit5_L1`. Connect through BitDIY, rather than only through Android's Bluetooth settings.

If an older F6 entry appears, use the active F7 entry. The virtual address and your physical device's macOS identifier are different identifiers for different sides of the bridge.

You can open the tested BitDIY app version from a terminal with:

```sh
adb -s emulator-5554 shell am start -n com.arixin.bitmaker/com.arixin.bitsensorctrlcenter.start.SplashActivity
```

If your APK has a different package or activity name, open it from Android's app launcher instead.

## 7. Check that data is flowing

In a separate terminal in the repository folder:

```sh
tail -f logs/bit5-live.log
```

| Log message | Meaning |
|---|---|
| `VIRTUAL_READY` | The virtual device started advertising. |
| `REAL_CONNECTED` | The Mac connected to your real Bit5. |
| `ANDROID_CONNECTION` | Android connected to the virtual device. |
| `FFF1_SUBSCRIPTION ... notify=True` | BitDIY enabled sensor notifications. |
| `ANDROID_TO_REAL` | A command is being forwarded to the physical device. |
| `ANDROID_NOTIFICATION_SENT` | Sensor bytes were sent toward Android. |
| `REAL_NOTIFICATION_NO_ANDROID_SUBSCRIBER` | Real data arrived, but no Android client enabled notifications. |
| `REAL_NOT_FOUND` | The selected physical device was not found; scanning will retry. |

Both connections and the notification subscription are needed for sensor values to reach BitDIY. A submitted BLE write is not proof that the physical device executed it.

## 8. Stop, restart or change devices

Press **Ctrl-C** in the terminal running the bridge. The main launcher leaves Pixel_7 open. Ctrl-C in a `tail -f` terminal stops only the log viewer.

Run only one virtual Bit5 process at a time. If the launcher says another instance is running, stop it in its terminal before starting a replacement.

To capture a second physical Bit5 without overwriting the first profile:

```sh
.venv/bin/python inspect_bit_device.py --output profiles/bit5-second.json
```

With Pixel_7 running and the old bridge stopped:

```sh
./start_bit5_second.sh
```

This second-device launcher starts the bridge only. It does not start the emulator. The main `./start_bit5.sh` continues to use the original profile.

For another profile filename, use:

```sh
.venv/bin/python bit5_live.py --profile profiles/your-device.json
```

Changing the profile selects another compatible Bit5; it does not add support for unrelated Bluetooth devices.

## Troubleshooting

| Problem | What to check |
|---|---|
| Missing `.venv` or Python module | Complete step 2 from the repository folder. |
| `Pixel_7` not found | Check the exact AVD name and SDK location in step 3. |
| `adb` reports `offline` | Wait for boot; if unresponsive, close Pixel_7 and relaunch it. |
| Netsim timeout | Close an existing Pixel_7 instance and start through the helper or main launcher. Check `logs/pixel7.log`. |
| Missing profile or `selected_device` | Complete the full capture in step 5. Profiles are not included in Git. |
| `REAL_NOT_FOUND` | Power on the selected Bit5, disconnect other apps, and ensure the profile was captured on this Mac. |
| BitDIY stays on “Connecting” | Refresh discovery and select the active F7 entry. Check for `ANDROID_CONNECTION` in the bridge log. |
| No sensor values | Check `REAL_CONNECTED` and `FFF1_SUBSCRIPTION ... notify=True`; then interact with the real sensor. |

The main launcher currently uses host graphics, disables Vulkan and snapshots, and uses two CPU cores. Audio stays enabled. Emulator stability varies; those settings are recovery choices, not a guarantee against crashes.

Microphone, speech recognition and TTS use Android's audio/network systems, separately from BLE. Host microphone input may need enabling in the emulator controls and macOS microphone permissions. A working BLE link does not confirm that speech services work.

## Further reading

- [How the bridge works](Bit5_BLE_Bridge_Overview.md)
- [Bumble Android/Netsim documentation](https://google.github.io/bumble/platforms/android.html)

No full Xcode installation or Swift code is required for the bridge. Creating the Android emulator does not require creating or building a new Android app project.
