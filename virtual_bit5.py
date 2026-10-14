#!/usr/bin/env python3
"""Profile-driven Bit5 observer on Bumble/Netsim; no physical BLE access."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import struct

from bumble import att
from bumble.core import AdvertisingData, UUID
from bumble.device import Device
from bumble.gatt import Characteristic, CharacteristicValue, Descriptor, Service
from bumble.transport import open_transport

LOG = logging.getLogger('virtual_bit5')
FFF1 = UUID('FFF1')
CCCD = UUID('2902')


def compact_uuid(value: str) -> UUID:
    uuid = UUID(value)
    # CoreBluetooth records Bluetooth-base UUIDs expanded to 128 bits.
    if value.lower().endswith('-0000-1000-8000-00805f9b34fb'):
        number = int(value[:8], 16)
        return UUID.from_16_bits(number) if number <= 0xFFFF else UUID.from_32_bits(number)
    return uuid


def advertising_payloads(profile: dict) -> tuple[str, bytes, bytes]:
    selected = profile['selected_device']
    advertisement = selected['advertisement']
    name = advertisement.get('local_name') or selected['name']
    fields = [(AdvertisingData.FLAGS, b'\x06')]
    grouped = {2: [], 4: [], 16: []}
    for value in advertisement.get('service_uuids', []):
        encoded = compact_uuid(value).to_bytes()
        grouped[len(encoded)].append(encoded)
    for width, ad_type in ((2, 0x03), (4, 0x05), (16, 0x07)):
        if grouped[width]:
            fields.append((ad_type, b''.join(grouped[width])))
    fields.append((AdvertisingData.COMPLETE_LOCAL_NAME, name.encode('utf-8')))
    if advertisement.get('tx_power') is not None:
        fields.append((AdvertisingData.TX_POWER_LEVEL, struct.pack('b', advertisement['tx_power'])))
    for company, value in advertisement.get('manufacturer_data', {}).items():
        fields.append((AdvertisingData.MANUFACTURER_SPECIFIC_DATA, struct.pack('<H', int(company)) + bytes.fromhex(value)))
    for uuid, value in advertisement.get('service_data', {}).items():
        encoded = compact_uuid(uuid).to_bytes()
        fields.append(({2: 0x16, 4: 0x20, 16: 0x21}[len(encoded)], encoded + bytes.fromhex(value)))
    primary, response = [], []
    for field in fields:
        size = len(field[1]) + 2
        if sum(len(item[1]) + 2 for item in primary) + size <= 31:
            primary.append(field)
        elif sum(len(item[1]) + 2 for item in response) + size <= 31:
            response.append(field)
        else:
            raise ValueError('Captured advertising fields exceed legacy advertisement and scan response capacity')
    return name, bytes(AdvertisingData(primary)), bytes(AdvertisingData(response))


def connection_info(bearer) -> str:
    connection = getattr(bearer, 'connection', bearer)
    return f"handle={getattr(connection, 'handle', '?')} peer={getattr(connection, 'peer_address', '?')}"


def install_att_logging(device: Device) -> None:
    server = device.gatt_server
    original_receive = server.on_gatt_pdu
    original_send = server.send_response

    def receive(bearer, pdu):
        LOG.info('ATT_RX %s type=%s length=%d hex=%s decoded=%s', connection_info(bearer), pdu.name, len(bytes(pdu)), bytes(pdu).hex(), str(pdu).replace('\n', ' '))
        if pdu.name in ('ATT_READ_BY_GROUP_TYPE_REQUEST', 'ATT_READ_BY_TYPE_REQUEST', 'ATT_FIND_INFORMATION_REQUEST', 'ATT_FIND_BY_TYPE_VALUE_REQUEST'):
            LOG.info('DISCOVERY_REQUEST %s type=%s', connection_info(bearer), pdu.name)
        handle = getattr(pdu, 'attribute_handle', None)
        attribute = server.get_attribute(handle) if handle is not None else None
        if attribute is not None and hasattr(pdu, 'attribute_value'):
            value = bytes(pdu.attribute_value)
            if attribute.type == FFF1:
                LOG.info('>>> FFF1_WRITE %s write_type=%s length=%d hex=%s', connection_info(bearer), pdu.name, len(value), value.hex())
            elif attribute.type == CCCD:
                LOG.info('CCCD_WRITE %s attribute_handle=%d write_type=%s length=%d hex=%s', connection_info(bearer), handle, pdu.name, len(value), value.hex())
        original_receive(bearer, pdu)

    def send(bearer, pdu):
        LOG.info('ATT_TX %s type=%s length=%d hex=%s decoded=%s', connection_info(bearer), pdu.name, len(bytes(pdu)), bytes(pdu).hex(), str(pdu).replace('\n', ' '))
        original_send(bearer, pdu)

    server.on_gatt_pdu = receive
    server.send_response = send


def add_profile_services(device: Device, profile: dict) -> None:
    property_flags = {'read': Characteristic.Properties.READ, 'notify': Characteristic.Properties.NOTIFY,
                      'indicate': Characteristic.Properties.INDICATE, 'write': Characteristic.Properties.WRITE,
                      'write-without-response': Characteristic.Properties.WRITE_WITHOUT_RESPONSE}
    for service in profile['gatt']['services']:
        characteristics = []
        for record in service['characteristics']:
            uuid = compact_uuid(record['uuid'])
            properties = Characteristic.Properties(0)
            for prop in record['properties']:
                properties |= property_flags[prop]
            permissions = 0
            if 'read' in record['properties']:
                permissions |= Characteristic.READABLE
            if {'write', 'write-without-response'} & set(record['properties']):
                permissions |= Characteristic.WRITEABLE
            if uuid == FFF1:
                def reject_read(bearer):
                    LOG.info('FFF1_READ %s rejected=ATT_READ_NOT_PERMITTED', connection_info(bearer))
                    raise att.ATT_Error(att.ATT_READ_NOT_PERMITTED_ERROR)

                def accept_write(bearer, value):
                    LOG.info('FFF1_WRITE_ACCEPTED %s length=%d hex=%s', connection_info(bearer), len(value), bytes(value).hex())

                value = CharacteristicValue(read=reject_read, write=accept_write)
            elif 'read_value_hex' in record:
                value = bytes.fromhex(record['read_value_hex'])
            else:
                raise ValueError(f'No captured runtime behavior/value for {uuid}')
            descriptors = []
            for descriptor in record.get('descriptors', []):
                if compact_uuid(descriptor['uuid']) == CCCD:
                    continue  # Bumble creates/manages CCCD from notify/indicate properties.
                if 'read_value_hex' not in descriptor:
                    raise ValueError(f'No captured value for descriptor {descriptor["uuid"]}')
                descriptors.append(Descriptor(compact_uuid(descriptor['uuid']), Descriptor.READABLE, bytes.fromhex(descriptor['read_value_hex'])))
            characteristic = Characteristic(uuid, properties, permissions, value, descriptors=descriptors)
            if uuid == FFF1:
                characteristic.on('subscription', lambda bearer, notify, indicate: LOG.info(
                    'FFF1_SUBSCRIPTION %s notify=%s indicate=%s', connection_info(bearer), notify, indicate))
            characteristics.append(characteristic)
        primary = service.get('primary')
        device.add_service(Service(compact_uuid(service['uuid']), characteristics, primary=True if primary is None else primary))
    for attribute in device.gatt_server.attributes:
        LOG.info('GATT_ATTRIBUTE %s', attribute)


def on_connection(connection):
    LOG.info('ANDROID_CONNECTION %s parameters=%s mtu=%s role=%s', connection_info(connection), connection.parameters, connection.att_mtu, connection.role)
    connection.on('disconnection', lambda reason: LOG.info('ANDROID_DISCONNECTION %s reason=%s', connection_info(connection), reason))
    connection.on('connection_parameters_update', lambda: LOG.info('CONNECTION_PARAMETERS %s parameters=%s', connection_info(connection), connection.parameters))
    connection.on('connection_att_mtu_update', lambda: LOG.info('ATT_MTU %s mtu=%s', connection_info(connection), connection.att_mtu))


async def run(args):
    profile = json.loads(args.profile.read_text())
    name, advertising, scan_response = advertising_payloads(profile)
    LOG.info('PROFILE name=%s source=%s real_identifier=%s advertised_services=%s', name, args.profile, profile['selected_device']['identifier'], profile['selected_device']['advertisement']['service_uuids'])
    LOG.info('ADVERTISING_PAYLOAD hex=%s scan_response_hex=%s virtual_address=%s', advertising.hex(), scan_response.hex(), args.address)
    LOG.info('Profile does not capture raw AD field types, flags, primary status, or radio address. Using discoverable flags, complete UUID lists, primary services, and a virtual address. Captured RSSI is receiver-dependent.')
    LOG.info('Opening transport=%s', args.transport)
    async with await open_transport(args.transport) as transport:
        device = Device.with_hci(name, args.address, transport.source, transport.sink)
        add_profile_services(device, profile)
        install_att_logging(device)
        device.on('connection', on_connection)
        device.advertising_data = advertising
        device.scan_response_data = scan_response
        await device.power_on()
        await device.start_advertising(auto_restart=True)
        if not device.is_advertising:
            raise RuntimeError('Advertising start completed but device is not advertising')
        LOG.info('ADVERTISING_STARTED active=%s name=%s address=%s transport=%s', device.is_advertising, name, device.random_address, args.transport)
        LOG.info('READY: Open BitDIY in Pixel_7 (emulator-5554), scan for %s and connect. Waiting for FFF1 writes. No application notifications will be sent.', name)
        await transport.source.terminated
        raise RuntimeError('Netsim transport terminated')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path('profiles/bit-device.json'))
    parser.add_argument('--transport', default='android-netsim')
    parser.add_argument('--address', default='F0:F1:F2:F3:F4:F6')
    parser.add_argument('--log-file', type=Path, default=Path('logs/virtual-bit5.log'))
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s',
                        datefmt='%Y-%m-%dT%H:%M:%S%z',
                        handlers=[logging.StreamHandler(), logging.FileHandler(args.log_file)])
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        LOG.info('Stopped by user')
    except Exception:
        LOG.exception('Virtual Bit5 stopped')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
