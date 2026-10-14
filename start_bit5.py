#!/usr/bin/env python3
"""Launch Pixel_7 and the live Bit5 bridge using the existing environment."""
import fcntl
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from bumble.transport.android_netsim import find_grpc_port

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / 'bit5-launch.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('The Bit5 launcher is already running. Use its terminal; stop it with Ctrl-C.', flush=True)
            return 1
        # Also detect bridges started directly, before creating a duplicate peripheral.
        processes = subprocess.run(['ps', '-axo', 'args='], capture_output=True, text=True, check=True).stdout
        if any(('python' in line.lower() and any(script in line.split() for script in ('bit5_live.py', 'virtual_bit5.py', str(ROOT / 'bit5_live.py'), str(ROOT / 'virtual_bit5.py')))) for line in processes.splitlines()):
            print('A Bit5 peripheral is already running. Stop that process with Ctrl-C before using this launcher.', flush=True)
            return 1
        sdk = Path(os.environ.get('ANDROID_SDK_ROOT') or os.environ.get('ANDROID_HOME') or Path.home() / 'Library/Android/sdk')
        adb = sdk / 'platform-tools/adb'
        if not adb.is_file():
            raise RuntimeError(f'ADB not found: {adb}')
        print('Starting/reusing Pixel_7. Emulator output: logs/pixel7.log', flush=True)
        with (logs / 'pixel7.log').open('a') as emulator_log:
            emulator = subprocess.Popen(
                [str(ROOT / 'launch_pixel7.sh'), '-gpu', 'host',
                 '-feature', '-Vulkan', '-no-snapshot', '-cores', '2'],
                stdout=emulator_log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 120
        print('Waiting for Pixel_7 and Netsim...', flush=True)
        while time.monotonic() < deadline:
            if emulator.poll() not in (None, 0):
                raise RuntimeError('Pixel_7 launch failed. See logs/pixel7.log')
            devices = subprocess.run([str(adb), 'devices'], capture_output=True, text=True, timeout=10).stdout
            pixel_ready = False
            for line in devices.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].startswith('emulator-') and parts[1] == 'device':
                    result = subprocess.run([str(adb), '-s', parts[0], 'emu', 'avd', 'name'], capture_output=True, text=True, timeout=10)
                    if 'Pixel_7' in result.stdout.splitlines():
                        pixel_ready = True
            port = find_grpc_port(0)
            if pixel_ready and port:
                try:
                    with socket.create_connection(('localhost', port), timeout=1):
                        break
                except OSError:
                    pass
            time.sleep(1)
        else:
            raise RuntimeError('Pixel_7/Netsim did not become ready within 120 seconds. See logs/pixel7.log; if Pixel_7 was already running without Netsim, close it and retry.')
        print('Starting live Bit5. Power on the real device; wait for REAL_CONNECTED, then connect in BitDIY. Ctrl-C stops the bridge and leaves Pixel_7 open.', flush=True)
        return subprocess.call([sys.executable, str(ROOT / 'bit5_live.py'), '--transport', f'android-netsim:localhost:{port}'])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nBit5 launcher stopped.')
        sys.exit(130)
    except Exception as error:
        print(f'Bit5 launch failed: {error}', file=sys.stderr)
        sys.exit(1)
