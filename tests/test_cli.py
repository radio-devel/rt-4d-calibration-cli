# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 rt-4d-calibration-cli contributors

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from rt4d_calibration_cli.cli import (
    ACK, CalibrationError, ENTER_MAGIC, EXIT_MAGIC, decode_capture, decode_sector,
    detect_port, format_report, load_config, next_cache_path,
    read_live_calibration,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".config" / "rt4d_oem_v325.yaml"
BLOCK_SIZE = 0x400


def config():
    return load_config(CONFIG)


def make_sector(marker=0x9A):
    data = bytearray([0xFF] * 0x1000)
    data[0] = marker
    data[1] = 3
    data[2:4] = (0xA037).to_bytes(2, "little")
    data[0x0C:0x0E] = (0x1234).to_bytes(2, "little")
    data[0x0E:0x10] = (0x2345).to_bytes(2, "little")
    data[0x10:0x1F] = bytes(range(30, 45))
    data[0x1F] = 130
    for index in range(15):
        base = 0x20 + index * 0x70
        data[base:base + 2] = (0x8000 + index - 7).to_bytes(2, "little")
        data[base + 2] = 10 + index
        data[base + 3:base + 5] = (0x1000 + index).to_bytes(2, "little")
        data[base + 0x10:base + 0x20] = bytes(range(index, index + 16))
        data[base + 0x20:base + 0x30] = bytes(range(index + 16, index + 32))
        data[base + 0x30:base + 0x70] = bytes([index]) * 0x40
    data[0x06B0:0x0800] = bytes(0x150)
    return bytes(data)


def checksum(data):
    return sum(data) & 0xFF


class FakePipe:
    def __init__(self, capture):
        self.timeout = 2.0
        self.capture = capture
        self.pending = bytearray()
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        if data == ENTER_MAGIC:
            self.pending.extend(ACK)
        elif data != EXIT_MAGIC:
            assert data[0] == 0x52 and data[-1] == checksum(data[:-1])
            block = int.from_bytes(data[1:3], "big")
            payload = self.capture[block * BLOCK_SIZE:(block + 1) * BLOCK_SIZE]
            reply = data[:3] + payload
            self.pending.extend(reply + bytes([checksum(reply)]))
        return len(data)

    def read(self, size):
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result


def fields_by_name(section):
    return {item["name"]: item for item in section["fields"]}


def test_decode_every_known_record_and_header():
    decoded = decode_sector(make_sector(), config(), "primary")
    assert decoded["valid_marker"] is True
    header = fields_by_name(decoded["header"])
    assert header["unknown_header_01"]["raw"] == 3
    assert "not been established" in header["unknown_header_01"]["description"]
    assert header["bk4819_reg_3e_preset"]["value"] == 0xA037
    assert header["bk4819_reg_3e_preset"]["word_hex"] == "0xA037"
    assert header["battery_adc_points"]["decoded_values"][0] == 3.0
    assert header["adc_correction"]["value"] == 2
    records = decoded["record_table"]["records"]
    assert len(records) == 15
    assert fields_by_name(records[0])["frequency_correction"]["scaled"] == -70
    assert fields_by_name(records[14])["frequency_correction"]["scaled"] == 70
    assert fields_by_name(records[1])["high_power_pa_bias"]["raw"] == list(range(1, 17))
    assert records[11]["oem_v3_25_selectable"] is False


def test_capture_selection_and_difference():
    report = decode_capture(make_sector(0) + make_sector(), config())
    assert report["primary_mirror_identical"] is False
    assert report["startup_selection"] == "mirror"
    text = format_report(report)
    assert "[primary]" in text and "[mirror]" in text
    assert "record 14 1240-1300 MHz" in text


def test_report_formats_scaled_values_and_words_for_people():
    text = format_report(decode_capture(make_sector(), config()))
    assert "raw=37a0 (word=0xA037, value=41015)" in text
    assert "decoded_values=[3, 3.1, 3.2, 3.3" in text
    assert "3.3000000000000003" not in text


def test_identical_mirror_is_not_printed_twice():
    sector = make_sector()
    report = decode_capture(sector + sector, config())
    text = format_report(report)
    assert report["primary_mirror_identical"] is True
    assert "byte-identical" in text
    assert text.count("record 00 136-174 MHz") == 1


def test_rejects_short_capture():
    with pytest.raises(CalibrationError):
        decode_capture(bytes(100), config())


def test_rejects_capture_ending_inside_mirror():
    with pytest.raises(CalibrationError, match="ends inside mirror sector.*need 8192.*got 5000"):
        decode_capture(make_sector() + bytes(904), config())


def test_invalid_primary_alone_does_not_claim_fallback_selection():
    report = decode_capture(make_sector(0), config())
    assert report["startup_selection"] is None
    assert "OEM_startup_selection=unknown" in format_report(report)


def test_both_invalid_sectors_select_compiled_fallback():
    report = decode_capture(make_sector(0) * 2, config())
    assert report["startup_selection"] == "compiled fallback"


def test_read_live_uses_only_read_frames():
    capture = make_sector() * 2
    pipe = FakePipe(capture)
    progress = []
    result = read_live_calibration(
        pipe, config(), lambda done, total: progress.append((done, total)))
    assert result == capture
    assert pipe.writes[0] == ENTER_MAGIC and pipe.writes[-1] == EXIT_MAGIC
    assert len(pipe.writes) == 10
    assert all(frame[0] == 0x52 for frame in pipe.writes[1:-1])
    assert progress[-1] == (8, 8)


def test_detect_port_prefers_single_persistent_device(tmp_path):
    device = tmp_path / "ttyUSB0"
    device.touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    link = by_id / "usb-programming-cable"
    link.symlink_to(device)
    assert detect_port(by_id, ["/dev/ttyUSB9"]) == str(link)


def test_detect_port_refuses_ambiguous_candidates(tmp_path):
    with pytest.raises(CalibrationError, match="multiple USB serial devices"):
        detect_port(tmp_path, ["/dev/ttyUSB0", "/dev/ttyACM0"])


def test_cache_path_is_created_and_automatic(tmp_path):
    cache = tmp_path / ".cache"
    path = next_cache_path(
        cache, datetime(2026, 9, 13, 12, 34, 56, 123456, timezone.utc))
    assert cache.is_dir()
    assert path.parent == cache
    assert path.name == "rt4d-calibration-20260913-123456-123456+0000.bin"


def test_json_report_is_serializable():
    encoded = json.dumps(decode_capture(make_sector() * 2, config()))
    assert hashlib.sha256(make_sector()).hexdigest() in encoded


def test_clone_local_launcher_help_runs():
    result = subprocess.run(
        [sys.executable, "rt4d-calibration", "--help"], cwd=ROOT,
        capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "schema-driven read-only" in result.stdout.lower()


def test_launcher_loads_adjacent_config_outside_clone_directory(tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(make_sector() * 2)
    result = subprocess.run(
        [sys.executable, str(ROOT / "rt4d-calibration"), "decode",
         str(capture)], cwd=tmp_path, capture_output=True, text=True,
        check=False)
    assert result.returncode == 0
    assert str(CONFIG.resolve()) in result.stderr
    assert "iRadio RT-4D / DM-UV4R calibration report" in result.stdout


def test_yaml_controls_names_offsets_and_descriptions(tmp_path):
    schema = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    schema["header"]["fields"][1].update({
        "name": "configurable_name", "label": "Configurable label",
        "description": "Configured entirely in YAML", "offset": 0x02,
        "type": "u16le",
    })
    custom = tmp_path / "custom.yaml"
    custom.write_text(yaml.safe_dump(schema), encoding="utf-8")
    field = decode_sector(make_sector(), load_config(custom), "primary")["header"]["fields"][1]
    assert field["name"] == "configurable_name"
    assert field["label"] == "Configurable label"
    assert field["description"] == "Configured entirely in YAML"
    assert field["offset"] == "0x0002" and field["raw"] == 0xA037


def test_schema_rejects_field_outside_record(tmp_path):
    schema = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    schema["record_table"]["fields"][0]["offset"] = 0x70
    custom = tmp_path / "bad.yaml"
    custom.write_text(yaml.safe_dump(schema), encoding="utf-8")
    with pytest.raises(CalibrationError, match="exceeds boundary"):
        load_config(custom)
