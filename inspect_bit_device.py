#!/usr/bin/env python3
"""Scan and characterize a Bit4/Bit5 BLE peripheral using Bleak only."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BIT_NAME = re.compile(r"bit", re.IGNORECASE)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def advertisement_record(device: Any, advertisement: Any) -> dict[str, Any]:
    name = device.name or advertisement.local_name
    return {
        "timestamp": timestamp(),
        "device_name": name,
        "identifier": device.address,
        "local_name": advertisement.local_name,
        "service_uuids": list(advertisement.service_uuids or []),
        "manufacturer_data": {
            str(company_id): bytes(value).hex()
            for company_id, value in (advertisement.manufacturer_data or {}).items()
        },
        "service_data": {
            str(uuid): bytes(value).hex()
            for uuid, value in (advertisement.service_data or {}).items()
        },
        "rssi": advertisement.rssi,
        "tx_power": getattr(advertisement, "tx_power", None),
        "is_connectable": getattr(advertisement, "is_connectable", None),
    }


def print_advertisement(record: dict[str, Any]) -> None:
    name = record["device_name"] or "<unnamed>"
    marker = " *** BIT DEVICE CANDIDATE ***" if any(
        BIT_NAME.search(value or "") for value in (record["device_name"], record["local_name"])
    ) else ""
    print(
        f"[{record['timestamp']}] ADVERTISEMENT{marker} "
        f"device_name={name!r} identifier={record['identifier']} "
        f"local_name={record['local_name']!r} "
        f"service_uuids={record['service_uuids']} "
        f"manufacturer_data={record['manufacturer_data']} "
        f"service_data={record['service_data']} rssi={record['rssi']}",
        flush=True,
    )


@dataclass
class ScannedPeripheral:
    device: Any
    latest_advertisement: dict[str, Any]
    advertisement_events: list[dict[str, Any]] = field(default_factory=list)


async def scan_for_devices(seconds: float) -> tuple[dict[str, ScannedPeripheral], list[dict[str, Any]]]:
    from bleak import BleakScanner

    discovered: dict[str, ScannedPeripheral] = {}
    all_advertisements: list[dict[str, Any]] = []

    def on_advertisement(device: Any, advertisement: Any) -> None:
        record = advertisement_record(device, advertisement)
        print_advertisement(record)
        all_advertisements.append(record)
        identifier = device.address
        peripheral = discovered.get(identifier)
        if peripheral is None:
            peripheral = ScannedPeripheral(device, record)
            discovered[identifier] = peripheral
        else:
            peripheral.device = device
            peripheral.latest_advertisement = record
        peripheral.advertisement_events.append(record)

    print(f"Stage 1: scanning with Bleak for {seconds:.1f} seconds ...", flush=True)
    async with BleakScanner(detection_callback=on_advertisement):
        await asyncio.sleep(seconds)
    print(
        f"Scan complete: {len(all_advertisements)} advertisement callbacks from "
        f"{len(discovered)} unique device identifiers.",
        flush=True,
    )
    return discovered, all_advertisements


def select_peripheral(discovered: dict[str, ScannedPeripheral]) -> ScannedPeripheral | None:
    entries = list(discovered.values())
    candidates = [
        peripheral
        for peripheral in entries
        if any(BIT_NAME.search(value or "") for value in (
            peripheral.latest_advertisement["device_name"],
            peripheral.latest_advertisement["local_name"],
        ))
    ]
    choices = candidates or entries
    if not choices:
        print("No BLE devices were discovered; there is nothing to select.")
        return None
    if not candidates:
        print("No name containing BIT was seen; showing all discovered devices.")

    print("\nSelect the Bit4/Bit5 device to inspect:")
    for index, peripheral in enumerate(choices, start=1):
        advertisement = peripheral.latest_advertisement
        name = advertisement["device_name"] or advertisement["local_name"] or "<unnamed>"
        print(
            f"  {index}. {name} | identifier={peripheral.device.address} "
            f"| RSSI={advertisement['rssi']} | services={advertisement['service_uuids']}"
        )
    print("  q. Save the scan and exit")

    while True:
        selection = input("Device number: ").strip()
        if selection.casefold() == "q":
            return None
        try:
            selected_index = int(selection)
        except ValueError:
            print("Enter a listed number or q.")
            continue
        if 1 <= selected_index <= len(choices):
            return choices[selected_index - 1]
        print("That number is not in the list.")


def utf8_if_printable(value: bytes) -> str | None:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if all(character.isprintable() or character in "\r\n\t" for character in text):
        return text
    return None


async def inspect_peripheral(
    peripheral: ScannedPeripheral,
    profile: dict[str, Any],
    listen_seconds: float,
    connect_timeout: float,
) -> None:
    from bleak import BleakClient

    device = peripheral.device
    advertisement = peripheral.latest_advertisement
    name = advertisement["device_name"] or advertisement["local_name"] or "<unnamed>"
    print(
        f"\nStage 2: connecting to {name!r} using the discovered BLEDevice object "
        f"(CoreBluetooth identifier {device.address}) ...",
        flush=True,
    )
    profile["selected_device"] = {
        "name": name,
        "identifier": device.address,
        "advertisement": advertisement,
        "advertisement_event_count": len(peripheral.advertisement_events),
    }
    profile["gatt"] = {"services": []}
    profile["notifications"] = []

    async with BleakClient(device, timeout=connect_timeout, pair=False) as client:
        print(f"Connected: {client.is_connected}", flush=True)
        services = list(client.services)
        for service in services:
            service_record: dict[str, Any] = {
                "uuid": str(service.uuid),
                "handle": service.handle,
                "primary": getattr(service, "primary", None),
                "characteristics": [],
            }
            profile["gatt"]["services"].append(service_record)
            print(
                f"SERVICE uuid={service.uuid} handle={service.handle} "
                f"primary={service_record['primary']}",
                flush=True,
            )

            for characteristic in service.characteristics:
                properties = sorted(str(prop) for prop in characteristic.properties)
                try:
                    max_write_size = characteristic.max_write_without_response_size
                except Exception as error:
                    max_write_size = None
                    print(
                        f"  Could not obtain max write-without-response size: {error}",
                        flush=True,
                    )
                characteristic_record: dict[str, Any] = {
                    "uuid": str(characteristic.uuid),
                    "handle": characteristic.handle,
                    "properties": properties,
                    "max_write_without_response_size": max_write_size,
                    "descriptors": [],
                }
                service_record["characteristics"].append(characteristic_record)
                print(
                    f"  CHARACTERISTIC uuid={characteristic.uuid} "
                    f"handle={characteristic.handle} properties={properties} "
                    f"max_write_without_response_size={max_write_size}",
                    flush=True,
                )

                for descriptor in characteristic.descriptors:
                    descriptor_record = {
                        "uuid": str(descriptor.uuid),
                        "handle": descriptor.handle,
                    }
                    characteristic_record["descriptors"].append(descriptor_record)
                    print(
                        f"    DESCRIPTOR uuid={descriptor.uuid} handle={descriptor.handle}",
                        flush=True,
                    )

                if "read" in {property_name.casefold() for property_name in properties}:
                    try:
                        value = bytes(await client.read_gatt_char(characteristic))
                        characteristic_record["read_value_hex"] = value.hex()
                        decoded = utf8_if_printable(value)
                        characteristic_record["read_value_utf8"] = decoded
                        print(f"    READ hex={value.hex()} utf8={decoded!r}", flush=True)
                    except Exception as error:
                        characteristic_record["read_error"] = str(error)
                        print(f"    READ failed: {error!r}", flush=True)

        notification_characteristics = [
            (service, characteristic)
            for service in services
            for characteristic in service.characteristics
            if {"notify", "indicate"}
            & {property_name.casefold() for property_name in characteristic.properties}
        ]
        print(
            f"\nStage 3: subscribing to {len(notification_characteristics)} "
            "notify/indicate characteristic(s) ...",
            flush=True,
        )
        for service, characteristic in notification_characteristics:
            service_record = next(
                item
                for item in profile["gatt"]["services"]
                if item["handle"] == service.handle
            )
            characteristic_record = next(
                item
                for item in service_record["characteristics"]
                if item["handle"] == characteristic.handle
            )

            def on_notification(
                sender: Any,
                data: bytearray,
                service_uuid: str = str(service.uuid),
                characteristic_uuid: str = str(characteristic.uuid),
            ) -> None:
                payload = bytes(data)
                event = {
                    "timestamp": timestamp(),
                    "service_uuid": service_uuid,
                    "characteristic_uuid": characteristic_uuid,
                    "characteristic_handle": sender.handle,
                    "length": len(payload),
                    "payload_hex": payload.hex(),
                }
                profile["notifications"].append(event)
                print(
                    f"[{event['timestamp']}] NOTIFICATION "
                    f"characteristic_uuid={characteristic_uuid} "
                    f"length={len(payload)} payload_hex={payload.hex()}",
                    flush=True,
                )

            try:
                await client.start_notify(characteristic, on_notification)
                characteristic_record["notification_subscription"] = "active"
                print(
                    f"  Subscribed: service={service.uuid} characteristic={characteristic.uuid}",
                    flush=True,
                )
            except Exception as error:
                characteristic_record["notification_subscription"] = "failed"
                characteristic_record["notification_subscription_error"] = str(error)
                print(
                    f"  Subscription failed: characteristic={characteristic.uuid} error={error!r}",
                    flush=True,
                )

        active_count = sum(
            characteristic.get("notification_subscription") == "active"
            for service in profile["gatt"]["services"]
            for characteristic in service["characteristics"]
        )
        print(
            f"\n{active_count} notification subscription(s) active. Interact physically with the Bit4/Bit5 now "
            f"(button presses, movement, or its normal trigger). Keeping the BLE "
            f"connection open for at least {listen_seconds:.1f} seconds.",
            flush=True,
        )
        await asyncio.sleep(listen_seconds)
        print(
            f"Notification window ended; received {len(profile['notifications'])} "
            "notification(s).",
            flush=True,
        )


def save_profile(profile: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"Saved machine-readable profile: {path}", flush=True)


def minimum_seconds(minimum: float):
    def parse(value: str) -> float:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum:g} seconds")
        return seconds

    return parse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Save advertisements without prompting or connecting",
    )
    parser.add_argument(
        "--scan-seconds",
        type=minimum_seconds(15),
        default=15.0,
        help="BLE scan duration, minimum 15 seconds (default: 15)",
    )
    parser.add_argument(
        "--listen-seconds",
        type=minimum_seconds(30),
        default=30.0,
        help="Notification listening window, minimum 30 seconds (default: 30)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=minimum_seconds(0.1),
        default=20.0,
        help="Connection timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("profiles/bit-device.json"),
        help="Profile output path (default: profiles/bit-device.json)",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    profile: dict[str, Any] = {
        "schema_version": 1,
        "platform": sys.platform,
        "scan_duration_seconds": args.scan_seconds,
        "advertisement_events": [],
        "discovered_devices": [],
        "gatt": {"services": []},
        "notifications": [],
    }
    try:
        discovered, advertisement_events = await scan_for_devices(args.scan_seconds)
        profile["advertisement_events"] = advertisement_events
        profile["discovered_devices"] = [
            {
                "name": peripheral.latest_advertisement["device_name"],
                "identifier": peripheral.device.address,
                "latest_advertisement": peripheral.latest_advertisement,
                "advertisement_event_count": len(peripheral.advertisement_events),
            }
            for peripheral in discovered.values()
        ]
        if args.scan_only:
            return
        selected = select_peripheral(discovered)
        if selected is None:
            return
        await inspect_peripheral(
            selected,
            profile,
            args.listen_seconds,
            args.connect_timeout,
        )
    except BaseException as error:
        profile["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        profile["finished_at"] = timestamp()
        save_profile(profile, args.output)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("Interrupted; saving the profile collected so far.", file=sys.stderr)
    except Exception as error:
        print(f"Diagnostic failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
