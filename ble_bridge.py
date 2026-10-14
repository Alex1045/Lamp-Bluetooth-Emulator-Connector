#!/usr/bin/env python3
"""Inspect a physical Bit4/Bit5 and optionally proxy it through Bumble/Netsim."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("bit_ble_bridge")
BIT_NAME = re.compile(r"bit\s*[45]", re.IGNORECASE)
DEFAULT_ADDRESS = "F0:F1:F2:F3:F4:F5"


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("bleak").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("bumble").setLevel(logging.DEBUG if verbose else logging.INFO)


def advertisement_json(advertisement: Any) -> dict[str, Any]:
    return {
        "local_name": advertisement.local_name,
        "rssi": advertisement.rssi,
        "tx_power": advertisement.tx_power,
        "service_uuids": list(advertisement.service_uuids or []),
        "service_data": {
            str(uuid): value.hex()
            for uuid, value in (advertisement.service_data or {}).items()
        },
        "manufacturer_data": {
            str(company_id): value.hex()
            for company_id, value in (advertisement.manufacturer_data or {}).items()
        },
        "platform_data": [str(item) for item in (advertisement.platform_data or [])],
    }


async def scan_devices(timeout: float, name_filter: str | None = None) -> list[tuple[Any, Any]]:
    from bleak import BleakScanner

    LOG.info("Scanning with macOS CoreBluetooth for %.1f seconds", timeout)
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    found: list[tuple[Any, Any]] = []
    for device, advertisement in devices.values():
        name = advertisement.local_name or device.name or "<unnamed>"
        if name_filter and name_filter.casefold() not in name.casefold():
            continue
        found.append((device, advertisement))
        LOG.info(
            "DEVICE name=%r identifier=%s rssi=%s services=%s manufacturer=%s service_data=%s",
            name,
            device.address,
            advertisement.rssi,
            advertisement.service_uuids,
            {key: value.hex() for key, value in (advertisement.manufacturer_data or {}).items()},
            {key: value.hex() for key, value in (advertisement.service_data or {}).items()},
        )
    if not found:
        LOG.warning("No matching BLE devices found")
    return found


async def choose_device(
    selector: str | None, timeout: float
) -> tuple[Any, Any]:
    devices = await scan_devices(timeout)
    if selector:
        matches = [
            item
            for item in devices
            if selector.casefold() in (item[1].local_name or item[0].name or "").casefold()
            or selector.casefold() == item[0].address.casefold()
        ]
    else:
        matches = [
            item
            for item in devices
            if BIT_NAME.search(item[1].local_name or item[0].name or "")
        ]
    if len(matches) != 1:
        candidates = [
            f"{item[1].local_name or item[0].name or '<unnamed>'} ({item[0].address})"
            for item in matches
        ]
        raise RuntimeError(
            f"Expected one matching device, found {len(matches)}: {candidates}. "
            "Rescan and pass --device with part of its advertised name or identifier."
        )
    return matches[0]


def bytes_value(value: bytes | bytearray | None) -> str | None:
    return None if value is None else bytes(value).hex()


async def inspect_profile(
    device: Any,
    advertisement: Any,
    observe_seconds: float,
) -> dict[str, Any]:
    from bleak import BleakClient

    profile: dict[str, Any] = {
        "device": {
            "name": advertisement.local_name or device.name,
            "identifier": device.address,
            "advertisement": advertisement_json(advertisement),
        },
        "services": [],
    }
    LOG.info("Connecting to %s (%s)", profile["device"]["name"], device.address)
    async with BleakClient(device, timeout=20.0, pair=False) as client:
        LOG.info("Connected: %s", client.is_connected)
        services = list(client.services)
        for service_index, service in enumerate(services):
            service_record: dict[str, Any] = {
                "uuid": str(service.uuid),
                "handle": service.handle,
                "primary": service.primary,
                "characteristics": [],
            }
            LOG.info("SERVICE[%d] uuid=%s handle=%s primary=%s", service_index, service.uuid, service.handle, service.primary)
            for char_index, characteristic in enumerate(service.characteristics):
                properties = sorted(str(value) for value in characteristic.properties)
                char_record: dict[str, Any] = {
                    "uuid": str(characteristic.uuid),
                    "handle": characteristic.handle,
                    "properties": properties,
                    "descriptors": [],
                }
                LOG.info(
                    "  CHARACTERISTIC[%d] uuid=%s handle=%s properties=%s",
                    char_index,
                    characteristic.uuid,
                    characteristic.handle,
                    properties,
                )
                if "read" in characteristic.properties:
                    try:
                        value = await client.read_gatt_char(characteristic)
                        char_record["read_value"] = bytes_value(value)
                        LOG.info("    READ value=%s", value.hex())
                    except Exception:
                        LOG.exception("    READ failed for %s", characteristic.uuid)
                for descriptor in characteristic.descriptors:
                    descriptor_record = {
                        "uuid": str(descriptor.uuid),
                        "handle": descriptor.handle,
                    }
                    LOG.info("    DESCRIPTOR uuid=%s handle=%s", descriptor.uuid, descriptor.handle)
                    try:
                        value = await client.read_gatt_descriptor(descriptor.handle)
                        descriptor_record["read_value"] = bytes_value(value)
                        LOG.info("      DESCRIPTOR READ value=%s", value.hex())
                    except Exception:
                        LOG.exception("      DESCRIPTOR READ failed for %s", descriptor.uuid)
                    char_record["descriptors"].append(descriptor_record)

                if observe_seconds > 0 and ({"notify", "indicate"} & set(characteristic.properties)):
                    def on_notification(sender: Any, data: bytearray, uuid: str = str(characteristic.uuid)) -> None:
                        LOG.info("NOTIFICATION uuid=%s sender=%s value=%s", uuid, sender, bytes(data).hex())

                    try:
                        await client.start_notify(characteristic, on_notification)
                        char_record["observed_notifications"] = True
                        LOG.info("    Notifications enabled for %.1f seconds", observe_seconds)
                    except Exception:
                        char_record["observed_notifications"] = False
                        LOG.exception("    Notification subscription failed for %s", characteristic.uuid)
                service_record["characteristics"].append(char_record)
            profile["services"].append(service_record)

        if observe_seconds > 0:
            LOG.info("Observing notifications for %.1f seconds", observe_seconds)
            await asyncio.sleep(observe_seconds)
        LOG.info("Disconnecting after GATT inspection")
    return profile


def save_profile(profile: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    LOG.info("Wrote GATT profile to %s", path)


def bumble_properties(properties: list[str]) -> tuple[Any, Any]:
    from bumble.gatt import Characteristic

    flag_map = {
        "broadcast": Characteristic.Properties.BROADCAST,
        "read": Characteristic.Properties.READ,
        "write-without-response": Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
        "write": Characteristic.Properties.WRITE,
        "notify": Characteristic.Properties.NOTIFY,
        "indicate": Characteristic.Properties.INDICATE,
        "authenticated-signed-writes": Characteristic.Properties.AUTHENTICATED_SIGNED_WRITES,
        "extended-properties": Characteristic.Properties.EXTENDED_PROPERTIES,
    }
    flags = Characteristic.Properties(0)
    for property_name in properties:
        flags |= flag_map.get(property_name, Characteristic.Properties(0))
    permissions = 0
    if "read" in properties:
        permissions |= Characteristic.READABLE
    if {"write", "write-without-response"} & set(properties):
        permissions |= Characteristic.WRITEABLE
    return flags, permissions


@dataclass
class RemoteCharacteristic:
    service_uuid: str
    characteristic: Any
    local_characteristic: Any


async def run_bumble_smoke(transport: str) -> None:
    from bumble.core import AdvertisingData, UUID
    from bumble import data_types
    from bumble.device import Device
    from bumble.gatt import Characteristic, Service
    from bumble.transport import open_transport

    LOG.info("Opening Bumble transport: %s", transport)
    async with await open_transport(transport) as hci_transport:
        device = Device.with_hci("Bumble BLE smoke", DEFAULT_ADDRESS, hci_transport.source, hci_transport.sink)
        device.add_service(
            Service(
                "FFF0",
                [Characteristic("FFF1", Characteristic.Properties.READ, Characteristic.READABLE, b"netsim-ok")],
            )
        )
        device.advertising_data = bytes(
            AdvertisingData(
                [
                    data_types.Flags(
                        AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
                        | AdvertisingData.Flags.BR_EDR_NOT_SUPPORTED
                    ),
                    data_types.CompleteLocalName("Bumble BLE smoke"),
                    data_types.IncompleteListOf16BitServiceUUIDs([UUID("FFF0")]),
                ]
            )
        )
        LOG.info("Smoke GATT service FFF0 / characteristic FFF1 added")
        await device.power_on()
        await device.start_advertising(auto_restart=True)
        LOG.info("Advertising over %s; scan from Pixel_7. Press Ctrl-C to stop.", transport)
        await hci_transport.source.terminated


async def run_proxy(args: argparse.Namespace) -> None:
    from bleak import BleakClient
    from bumble.core import UUID
    from bumble.device import Device
    from bumble.gatt import Characteristic, CharacteristicValue, Descriptor, Service
    from bumble.transport import open_transport

    device_info, advertisement = await choose_device(args.device, args.scan_timeout)
    remote_name = advertisement.local_name or device_info.name or "Bit4/Bit5"
    LOG.info("Connecting to physical peripheral %r", remote_name)

    async with BleakClient(device_info, timeout=20.0, pair=False) as client:
        LOG.info("Physical peripheral connected: %s", client.is_connected)
        services = list(client.services)
        LOG.info("Discovered %d services", len(services))
        for service in services:
            LOG.info("REMOTE SERVICE uuid=%s handle=%s", service.uuid, service.handle)
            for characteristic in service.characteristics:
                LOG.info(
                    "REMOTE CHARACTERISTIC service=%s uuid=%s handle=%s properties=%s",
                    service.uuid,
                    characteristic.uuid,
                    characteristic.handle,
                    sorted(characteristic.properties),
                )
                for descriptor in characteristic.descriptors:
                    LOG.info("REMOTE DESCRIPTOR uuid=%s handle=%s", descriptor.uuid, descriptor.handle)
        async with await open_transport(args.transport) as hci_transport:
            virtual = Device.with_hci(
                remote_name,
                DEFAULT_ADDRESS,
                hci_transport.source,
                hci_transport.sink,
            )
            remote_entries: list[RemoteCharacteristic] = []
            local_services = []

            for service in services:
                local_chars = []
                if UUID(str(service.uuid)) in (UUID("1800"), UUID("1801")):
                    LOG.info(
                        "Using Bumble's built-in standard service instead of duplicating %s",
                        service.uuid,
                    )
                    continue
                for remote_char in service.characteristics:
                    flags, permissions = bumble_properties(list(remote_char.properties))

                    async def read_remote(_connection: Any, remote: Any = remote_char) -> bytes:
                        LOG.info("ANDROID READ -> physical uuid=%s handle=%s", remote.uuid, remote.handle)
                        value = bytes(await client.read_gatt_char(remote))
                        LOG.info("PHYSICAL READ <- value=%s", value.hex())
                        return value

                    async def write_remote(_connection: Any, value: bytes, remote: Any = remote_char) -> None:
                        LOG.info("ANDROID WRITE -> physical uuid=%s handle=%s value=%s", remote.uuid, remote.handle, bytes(value).hex())
                        response = "write" in remote.properties
                        await client.write_gatt_char(remote, bytes(value), response=response)
                        LOG.info("PHYSICAL WRITE completed uuid=%s response=%s", remote.uuid, response)

                    local_descriptors = []
                    for remote_descriptor in remote_char.descriptors:
                        if remote_descriptor.uuid.lower() == "00002902-0000-1000-8000-00805f9b34fb":
                            continue

                        async def read_descriptor(_connection: Any, remote: Any = remote_descriptor) -> bytes:
                            LOG.info("ANDROID DESCRIPTOR READ -> physical uuid=%s handle=%s", remote.uuid, remote.handle)
                            value = bytes(await client.read_gatt_descriptor(remote.handle))
                            LOG.info("PHYSICAL DESCRIPTOR READ <- value=%s", value.hex())
                            return value

                        async def write_descriptor(_connection: Any, value: bytes, remote: Any = remote_descriptor) -> None:
                            LOG.info("ANDROID DESCRIPTOR WRITE -> physical uuid=%s handle=%s value=%s", remote.uuid, remote.handle, bytes(value).hex())
                            await client.write_gatt_descriptor(remote.handle, bytes(value))
                            LOG.info("PHYSICAL DESCRIPTOR WRITE completed uuid=%s", remote.uuid)

                        try:
                            local_descriptors.append(
                                Descriptor(
                                    str(remote_descriptor.uuid),
                                    Descriptor.READABLE | Descriptor.WRITEABLE,
                                    CharacteristicValue(read=read_descriptor, write=write_descriptor),
                                )
                            )
                        except Exception:
                            LOG.exception("Could not mirror descriptor %s", remote_descriptor.uuid)
                    local_char = Characteristic(
                        str(remote_char.uuid),
                        flags,
                        permissions,
                        CharacteristicValue(read=read_remote, write=write_remote),
                        descriptors=local_descriptors,
                    )
                    local_chars.append(local_char)
                    entry = RemoteCharacteristic(str(service.uuid), remote_char, local_char)
                    remote_entries.append(entry)

                    def on_subscription(
                        _connection: Any,
                        notify_enabled: bool,
                        indicate_enabled: bool,
                        selected: RemoteCharacteristic = entry,
                    ) -> None:
                        enabled = notify_enabled or indicate_enabled
                        LOG.info(
                            "ANDROID SUBSCRIPTION service=%s characteristic=%s notify=%s indicate=%s",
                            selected.service_uuid,
                            selected.characteristic.uuid,
                            notify_enabled,
                            indicate_enabled,
                        )
                        if not ({"notify", "indicate"} & set(selected.characteristic.properties)):
                            return

                        async def update_subscription() -> None:
                            try:
                                if enabled:
                                    async def on_notification(_sender: Any, data: bytearray, item: RemoteCharacteristic = selected) -> None:
                                        value = bytes(data)
                                        LOG.info("PHYSICAL NOTIFICATION -> ANDROID service=%s characteristic=%s value=%s", item.service_uuid, item.characteristic.uuid, value.hex())
                                        await virtual.notify_subscribers(item.local_characteristic, value)

                                    await client.start_notify(selected.characteristic, on_notification)
                                    LOG.info("Physical notifications enabled for %s", selected.characteristic.uuid)
                                else:
                                    await client.stop_notify(selected.characteristic)
                                    LOG.info("Physical notifications disabled for %s", selected.characteristic.uuid)
                            except Exception:
                                LOG.exception("Notification subscription update failed for %s", selected.characteristic.uuid)

                        asyncio.create_task(update_subscription())

                    local_char.on("subscription", on_subscription)
                local_services.append(Service(str(service.uuid), local_chars, primary=service.primary))

            virtual.add_services(local_services)
            virtual.advertising_data = make_advertising_data(remote_name, advertisement, services)
            LOG.info("Built Bumble mirror with %d services and %d characteristics", len(local_services), len(remote_entries))
            for attribute in virtual.gatt_server.attributes:
                LOG.debug("BUMBLE ATTRIBUTE %s", attribute)

            await virtual.power_on()
            await virtual.start_advertising(auto_restart=True)
            LOG.info("Proxy advertising as %r over %s. Press Ctrl-C to stop.", remote_name, args.transport)
            await hci_transport.source.terminated


def make_advertising_data(name: str, advertisement: Any, services: list[Any]) -> bytes:
    from bumble import data_types
    from bumble.core import UUID
    from bumble.core import AdvertisingData

    elements: list[Any] = [
        data_types.Flags(
            AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
            | AdvertisingData.Flags.BR_EDR_NOT_SUPPORTED
        )
    ]
    encoded_name = name.encode("utf-8")
    if len(encoded_name) <= 26:
        elements.append(data_types.CompleteLocalName(name))
    else:
        short_name = encoded_name[:26].decode("utf-8", errors="ignore")
        elements.append(data_types.ShortenedLocalName(short_name))

    service_uuids: dict[int, list[Any]] = {2: [], 4: [], 16: []}
    seen: set[str] = set()
    for uuid in list(advertisement.service_uuids or []) + [str(service.uuid) for service in services]:
        if uuid.casefold() not in seen:
            seen.add(uuid.casefold())
            bumble_uuid = UUID(uuid)
            uuid_size = len(bumble_uuid.uuid_bytes)
            if uuid_size in service_uuids:
                service_uuids[uuid_size].append(bumble_uuid)
    list_types = {
        2: data_types.IncompleteListOf16BitServiceUUIDs,
        4: data_types.IncompleteListOf32BitServiceUUIDs,
        16: data_types.IncompleteListOf128BitServiceUUIDs,
    }
    for uuid_size, uuids in service_uuids.items():
        if uuids:
            elements.append(list_types[uuid_size](uuids))
    if advertisement.tx_power is not None:
        elements.append(data_types.TxPowerLevel(advertisement.tx_power))
    appearance = getattr(advertisement, "appearance", None)
    if appearance is not None:
        elements.append(data_types.Appearance.from_int(appearance))
    for company_id, value in (advertisement.manufacturer_data or {}).items():
        elements.append(data_types.ManufacturerSpecificData(company_id, bytes(value)))
    service_data_types = {
        2: data_types.ServiceData16BitUUID,
        4: data_types.ServiceData32BitUUID,
        16: data_types.ServiceData128BitUUID,
    }
    for service_uuid, value in (advertisement.service_data or {}).items():
        bumble_uuid = UUID(service_uuid)
        service_data_type = service_data_types.get(len(bumble_uuid.uuid_bytes))
        if service_data_type:
            elements.append(service_data_type(bumble_uuid, bytes(value)))

    kept: list[Any] = []
    for element in elements:
        candidate = bytes(AdvertisingData([*kept, element]))
        if len(candidate) <= 31:
            kept.append(element)
        else:
            LOG.warning(
                "Omitting advertising element %s (%d-byte payload exceeds legacy limit)",
                element,
                len(candidate),
            )
    return bytes(AdvertisingData(kept))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Enable detailed Bleak and Bumble logs")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Scan and list BLE advertisements")
    scan.add_argument("--timeout", type=float, default=10.0)
    scan.add_argument("--filter", dest="name_filter", help="Only show names containing this text")

    inspect = commands.add_parser("inspect", help="Connect, enumerate GATT, read values, and observe notifications")
    inspect.add_argument("--device", help="Name fragment or macOS BLE identifier")
    inspect.add_argument("--scan-timeout", type=float, default=10.0)
    inspect.add_argument("--observe-seconds", type=float, default=5.0)
    inspect.add_argument("--output", type=Path, default=Path("profiles/bit-device.json"))

    smoke = commands.add_parser("bumble-smoke", help="Advertise a test GATT service through Android Netsim")
    smoke.add_argument("--transport", default="android-netsim")

    proxy = commands.add_parser("proxy", help="Mirror one Bit4/Bit5 peripheral through Bumble/Netsim")
    proxy.add_argument("--device", help="Name fragment or macOS BLE identifier")
    proxy.add_argument("--scan-timeout", type=float, default=10.0)
    proxy.add_argument("--transport", default="android-netsim")
    return parser


async def async_main(args: argparse.Namespace) -> None:
    if args.command == "scan":
        await scan_devices(args.timeout, args.name_filter)
    elif args.command == "inspect":
        device, advertisement = await choose_device(args.device, args.scan_timeout)
        profile = await inspect_profile(device, advertisement, args.observe_seconds)
        save_profile(profile, args.output)
    elif args.command == "bumble-smoke":
        await run_bumble_smoke(args.transport)
    elif args.command == "proxy":
        await run_proxy(args)


def main() -> None:
    args = make_parser().parse_args()
    configure_logging(args.verbose)
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        LOG.info("Stopped by user")
    except Exception as error:
        LOG.exception("Bridge stopped: %s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()