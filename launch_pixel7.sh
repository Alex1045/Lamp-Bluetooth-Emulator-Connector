#!/bin/sh
set -eu

SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
EMULATOR="$SDK_ROOT/emulator/emulator"
ADB="$SDK_ROOT/platform-tools/adb"

if [ ! -x "$EMULATOR" ]; then
    printf 'Android emulator not found at %s\n' "$EMULATOR" >&2
    printf 'Set ANDROID_SDK_ROOT or install the Android Emulator component in Android Studio.\n' >&2
    exit 1
fi

if ! "$EMULATOR" -list-avds | grep -Fxq 'Pixel_7'; then
    printf 'AVD Pixel_7 was not found. Available AVDs:\n' >&2
    "$EMULATOR" -list-avds >&2
    exit 1
fi

if [ -x "$ADB" ]; then
    for serial in $("$ADB" devices | awk '/^emulator-/{print $1}'); do
        avd_info=$("$ADB" -s "$serial" emu avd name 2>/dev/null || true)
        if printf '%s\n' "$avd_info" | grep -Fq 'Pixel_7'; then
            printf 'Pixel_7 is already running on %s; reusing it.\n' "$serial"
            printf 'For a guaranteed Netsim launch, stop that AVD and rerun this script.\n'
            exit 0
        fi
    done
fi

exec "$EMULATOR" -avd Pixel_7 -packet-streamer-endpoint default "$@"