"""Read-only adapter: redaction, stable identity and wire compatibility."""

import argparse
import json

from app.security.mac_observer import event, reducer_args, usb_devices


def test_nested_usb_devices_have_hashed_identifiers_and_no_serials():
    rows = [{"IORegistryEntryChildren": [{"idVendor": 1452, "idProduct": 123,
              "locationID": 1, "USB Serial Number": "PRIVATE-SERIAL",
              "IORegistryEntryChildren": [{"idVendor": 111, "idProduct": 222, "locationID": 2}]}]}]
    result = usb_devices(rows)
    assert len(result) == 2
    assert all(key.startswith("usb:sha256:") and len(key) == 75 for key in result)
    assert "PRIVATE-SERIAL" not in json.dumps(result)
    assert "Serial" not in json.dumps(result)


def test_non_usb_nodes_and_empty_registry_do_not_create_devices():
    assert usb_devices([]) == {}
    assert usb_devices([{"idVendor": 123}, {"IORegistryEntryChildren": [{"idProduct": 1}]}]) == {}


def test_macos_archive_with_dictionary_root_is_supported():
    root = {"IORegistryEntryChildren": [{"idVendor": 123, "idProduct": 456, "locationID": 1}]}
    assert len(usb_devices(root)) == 1


def test_device_identity_is_stable_but_different_ports_remain_distinct():
    first = {"idVendor": 123, "idProduct": 456, "locationID": 1}
    second = {**first, "locationID": 2}
    assert usb_devices([first]) == usb_devices([dict(reversed(list(first.items())))])
    assert set(usb_devices([first])).isdisjoint(usb_devices([second]))


def test_actual_events_cannot_be_mislabeled_synthetic_and_match_reducer_order():
    args = argparse.Namespace(workstation="desk", session="session", device="mac")
    observation = event("usb_added", {"deviceKey": "usb:sha256:test"}, args, 7)
    wire = reducer_args(observation)
    assert observation["simulated"] is False
    assert observation["source"] == "mac"
    assert wire[:5] == ["desk", "session", "mac", observation["event_id"], "usb_added"]
    assert wire[6] == 7
    assert json.loads(wire[8]) == {"deviceKey": "usb:sha256:test"}
    assert wire[9:] == [False, 1]
