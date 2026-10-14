#!/usr/bin/env python3
"""Live Bit5 FFF1 bridge; forward actual Android commands without inventing payloads."""
import argparse
import asyncio
import json
import logging
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bumble.device import Device
from bumble.gatt import CharacteristicValue
from bumble.transport import open_transport
from virtual_bit5 import (FFF1, advertising_payloads, add_profile_services,
                          install_att_logging, on_connection, connection_info)

LOG = logging.getLogger('bit5_live')

async def maintain_advertising(virtual, last_refresh, now):
    """Reissue controller commands while idle, even if cached state says enabled."""
    if virtual.connections:
        return now
    if not virtual.is_advertising or now - last_refresh >= 30:
        await virtual.stop_advertising()
        # A connection can arrive while the stop command is in flight.
        if not virtual.connections:
            await virtual.start_advertising(auto_restart=True)
            LOG.info('ADVERTISING_REFRESHED active=%s', virtual.is_advertising)
        return now
    return last_refresh

async def forward_notification(server, characteristic, payload):
    """Preserve all stream bytes across bearers with different ATT MTUs."""
    recipients = [
        bearer for bearer, subscriptions in list(server.subscribers.items())
        if subscriptions.get(characteristic.handle, b'\x00')[0] & 1
    ]
    if not recipients:
        LOG.info('REAL_NOTIFICATION_NO_ANDROID_SUBSCRIBER length=%d hex=%s', len(payload), payload.hex())
    for bearer in recipients:
        limit = bearer.att_mtu - 3
        for offset in range(0, len(payload), limit):
            chunk = payload[offset:offset + limit]
            await server.notify_subscriber(bearer, characteristic, chunk)
            LOG.info('ANDROID_NOTIFICATION_SENT %s mtu=%d offset=%d length=%d hex=%s',
                     connection_info(bearer), bearer.att_mtu, offset, len(chunk), chunk.hex())

async def run(args):
    profile = json.loads(args.profile.read_text())
    selected = profile['selected_device']
    name, advertising, scan_response = advertising_payloads(profile)
    state = {'client': None, 'characteristic': None}
    real_ready = asyncio.Event()
    write_lock = asyncio.Lock()
    notification_lock = asyncio.Lock()
    async with await open_transport(args.transport) as transport:
        virtual = Device.with_hci(name, args.address, transport.source, transport.sink)
        add_profile_services(virtual, profile)
        local = next(attribute for attribute in virtual.gatt_server.attributes if attribute.type == FFF1)
        observed_read = local.value.read

        async def forward_write(bearer, value):
            payload = bytes(value)
            async with write_lock:
                client, remote = state['client'], state['characteristic']
                if client is None or not client.is_connected:
                    LOG.info('WRITE_WAITING_FOR_REAL %s length=%d', connection_info(bearer), len(payload))
                    try:
                        await asyncio.wait_for(real_ready.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        LOG.warning('REAL_OFFLINE_WRITE_DROPPED %s length=%d hex=%s', connection_info(bearer), len(payload), payload.hex())
                        return
                    client, remote = state['client'], state['characteristic']
                    if client is None or not client.is_connected or virtual.connections.get(bearer.handle) is not bearer:
                        LOG.warning('WRITE_DROPPED_CONNECTION_CHANGED length=%d hex=%s', len(payload), payload.hex())
                        return
                LOG.info('ANDROID_TO_REAL %s length=%d hex=%s response=False', connection_info(bearer), len(payload), payload.hex())
                try:
                    await client.write_gatt_char(remote, payload, response=False)
                    LOG.info('REAL_WRITE_SUBMITTED length=%d hex=%s (device execution not confirmed)', len(payload), payload.hex())
                except Exception:
                    LOG.exception('REAL_WRITE_FAILED length=%d hex=%s', len(payload), payload.hex())

        async def notify(sender, data):
            payload = bytes(data)
            LOG.info('REAL_TO_ANDROID length=%d hex=%s', len(payload), payload.hex())
            async with notification_lock:
                await forward_notification(virtual.gatt_server, local, payload)

        async def maintain_real_connection():
            while True:
                try:
                    LOG.info('REAL_SCAN duration=15 identifier=%s', selected['identifier'])
                    device = await BleakScanner.find_device_by_filter(
                        lambda device, advertisement: device.address == selected['identifier'], timeout=15)
                    if device is None:
                        LOG.warning('REAL_NOT_FOUND: virtual remains available; power on Bit5 and enable advertising')
                        await asyncio.sleep(5)
                        continue
                    disconnected = asyncio.Event()
                    async with BleakClient(device, timeout=20, disconnected_callback=lambda _: disconnected.set()) as client:
                        remote = client.services.get_characteristic('0000fff1-0000-1000-8000-00805f9b34fb')
                        if remote is None or 'write-without-response' not in remote.properties:
                            raise RuntimeError('Real FFF1 write-without-response characteristic not found')
                        await client.start_notify(remote, notify)
                        state.update(client=client, characteristic=remote)
                        real_ready.set()
                        LOG.info('REAL_CONNECTED name=%s identifier=%s FFF1_handle=%s notifications=active', selected['name'], device.address, remote.handle)
                        await disconnected.wait()
                        LOG.warning('REAL_DISCONNECTED: virtual remains available; reconnecting')
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception('REAL_CONNECTION_FAILED: virtual remains available; retrying')
                finally:
                    real_ready.clear()
                    state.update(client=None, characteristic=None)
                await asyncio.sleep(5)

        local.value = CharacteristicValue(read=observed_read, write=forward_write)
        install_att_logging(virtual)
        virtual.on('connection', on_connection)
        virtual.advertising_data = advertising
        virtual.scan_response_data = scan_response
        virtual.advertising_interval_min = 100
        virtual.advertising_interval_max = 150
        await virtual.power_on()
        await virtual.start_advertising(auto_restart=True)
        LOG.info('VIRTUAL_READY advertising=%s name=%s address=%s advertised_service=FFE0 gatt_service=FFF0', virtual.is_advertising, name, virtual.random_address)
        real_task = asyncio.create_task(maintain_real_connection())
        loop = asyncio.get_running_loop()
        last_refresh = last_status = loop.time()
        try:
            while not transport.source.terminated.done():
                if real_task.done():
                    await real_task
                    raise RuntimeError('Physical connection worker stopped')
                now = loop.time()
                last_refresh = await maintain_advertising(virtual, last_refresh, now)
                if now - last_status >= 15:
                    LOG.info('BRIDGE_STATUS advertising=%s android_connections=%d real_connected=%s notification_subscribers=%d',
                             virtual.is_advertising, len(virtual.connections), real_ready.is_set(),
                             sum(bool(subscriptions.get(local.handle, b'\x00')[0] & 1)
                                 for subscriptions in virtual.gatt_server.subscribers.values()))
                    last_status = now
                await asyncio.sleep(1)
            raise RuntimeError('Netsim transport terminated')
        finally:
            real_task.cancel()
            await asyncio.gather(real_task, return_exceptions=True)

async def supervise(args):
    delay = 2
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await run(args)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception('BRIDGE_RESTART: recreating Netsim peripheral in %d seconds', delay)
        if asyncio.get_running_loop().time() - started > 60:
            delay = 2
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path('profiles/bit-device.json'))
    parser.add_argument('--transport', default='android-netsim')
    parser.add_argument('--address', default='F0:F1:F2:F3:F4:F7',
                        help='Virtual static random address; changed to avoid stale Android F6 connection state')
    args = parser.parse_args()
    # Fail immediately on invalid input rather than retrying a broken profile forever.
    advertising_payloads(json.loads(args.profile.read_text()))
    Path('logs').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler('logs/bit5-live.log')])
    try:
        asyncio.run(supervise(args))
    except KeyboardInterrupt:
        LOG.info('Stopped')
    except Exception:
        LOG.exception('Live bridge failed')
        raise SystemExit(1)

if __name__ == '__main__':
    main()
