# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 rt-4d-calibration-cli contributors

"""Schema-driven, read-only RT-4D / DM-UV4R calibration CLI."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

import yaml


READ_OPCODE = 0x52
ENTER_MAGIC = bytes.fromhex("34 52 05 10 9b")
EXIT_MAGIC = bytes.fromhex("34 52 05 ee 79")
ACK = b"\x06"
DEFAULT_CONFIG_ENV = "RT4D_CALIBRATION_CONFIG"
DEFAULT_CACHE_ENV = "RT4D_CALIBRATION_CACHE"
SUPPORTED_TYPES = {"u8", "u16le", "u8_array", "bytes"}


class CalibrationError(Exception):
    """A schema, capture, protocol, or invocation error."""


def default_config_path() -> Path:
    configured = os.environ.get(DEFAULT_CONFIG_ENV)
    if configured:
        return Path(configured)
    candidates = (
        Path(__file__).resolve().parents[2] / ".config" / "rt4d_oem_v325.yaml",
        Path.cwd() / ".config" / "rt4d_oem_v325.yaml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise CalibrationError(
        "default schema not found; run the clone-local ./rt4d-calibration "
        "launcher or pass --config")


def default_cache_dir() -> Path:
    configured = os.environ.get(DEFAULT_CACHE_ENV)
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / ".cache"


def detect_port(by_id_dir: Path = Path("/dev/serial/by-id"),
                fallback_devices: Sequence[str] | None = None) -> str:
    """Select one serial adapter without opening or probing any port."""
    by_id = sorted(str(path) for path in by_id_dir.glob("*")
                   if path.exists() or path.is_symlink())
    if len(by_id) == 1:
        return by_id[0]
    if len(by_id) > 1:
        raise CalibrationError(
            "multiple persistent serial devices found; pass --port:\n  " +
            "\n  ".join(by_id))

    if fallback_devices is None:
        try:
            from serial.tools import list_ports
        except ImportError as exc:
            raise CalibrationError(
                "no /dev/serial/by-id device found and pyserial is unavailable") from exc
        fallback_devices = [port.device for port in list_ports.comports()
                            if Path(port.device).name.startswith(("ttyUSB", "ttyACM"))]
    fallback = sorted(set(fallback_devices))
    if len(fallback) == 1:
        return fallback[0]
    if len(fallback) > 1:
        raise CalibrationError(
            "multiple USB serial devices found; pass --port:\n  " +
            "\n  ".join(fallback))
    raise CalibrationError(
        "no USB serial adapter detected; connect the programming cable or pass --port")


def next_cache_path(cache_dir: Path, now: datetime | None = None) -> Path:
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S-%f%z")
    base = cache_dir / f"rt4d-calibration-{timestamp}.bin"
    if not base.exists() and not Path(str(base) + ".part").exists():
        return base
    for suffix in range(1, 1000):
        candidate = cache_dir / f"rt4d-calibration-{timestamp}-{suffix:03d}.bin"
        if not candidate.exists() and not Path(str(candidate) + ".part").exists():
            return candidate
    raise CalibrationError("could not allocate a unique cache filename")


def _int(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CalibrationError(f"{context} must be an integer")
    return value


def _field_size(field: Mapping[str, Any]) -> int:
    kind = field.get("type")
    if kind == "u8":
        return 1
    if kind == "u16le":
        return 2
    if kind in {"u8_array", "bytes"}:
        return _int(field.get("count"), f"{field.get('name')}.count")
    raise CalibrationError(
        f"field {field.get('name')} type {kind!r} is unsupported; "
        f"choose from {sorted(SUPPORTED_TYPES)}")


def _validate_fields(fields: Any, boundary: int, context: str) -> None:
    if not isinstance(fields, list):
        raise CalibrationError(f"{context}.fields must be a list")
    names = set()
    for field in fields:
        if not isinstance(field, dict):
            raise CalibrationError(f"{context} field entries must be mappings")
        for key in ("name", "label", "description", "offset", "type"):
            if key not in field:
                raise CalibrationError(f"{context} field is missing {key}")
        if field["name"] in names:
            raise CalibrationError(f"duplicate {context} field {field['name']}")
        names.add(field["name"])
        offset = _int(field["offset"], f"{context}.{field['name']}.offset")
        size = _field_size(field)
        if offset < 0 or size <= 0 or offset + size > boundary:
            raise CalibrationError(
                f"{context}.{field['name']} range 0x{offset:X}+0x{size:X} "
                f"exceeds boundary 0x{boundary:X}")


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise CalibrationError("YAML root must be a mapping")
    if config.get("schema_version") != 1:
        raise CalibrationError("only schema_version 1 is supported")

    transport = config.get("transport")
    if not isinstance(transport, dict):
        raise CalibrationError("transport must be a mapping")
    block_size = _int(transport.get("block_size"), "transport.block_size")
    read_start = _int(transport.get("read_start"), "transport.read_start")
    read_size = _int(transport.get("read_size"), "transport.read_size")
    if block_size <= 0 or read_start < 0 or read_size <= 0:
        raise CalibrationError("transport geometry is invalid")
    if read_start % block_size or read_size % block_size:
        raise CalibrationError("transport live-read range must be block-aligned")

    sectors = config.get("sectors")
    if not isinstance(sectors, list) or not sectors:
        raise CalibrationError("sectors must be a non-empty list")
    names = set()
    for sector in sectors:
        if not isinstance(sector, dict):
            raise CalibrationError("sector entries must be mappings")
        for key in ("name", "description", "offset", "size"):
            if key not in sector:
                raise CalibrationError(f"sector is missing {key}")
        name = sector["name"]
        if name in names:
            raise CalibrationError(f"duplicate sector name {name}")
        names.add(name)
        offset = _int(sector["offset"], f"sector {name}.offset")
        size = _int(sector["size"], f"sector {name}.size")
        if size <= 0 or offset < read_start or offset + size > read_start + read_size:
            raise CalibrationError(f"sector {name} lies outside transport range")

    selection = config.get("selection")
    if not isinstance(selection, dict):
        raise CalibrationError("selection must be a mapping")
    for key in ("marker_field", "valid_value", "priority", "fallback"):
        if key not in selection:
            raise CalibrationError(f"selection is missing {key}")
    if any(name not in names for name in selection["priority"]):
        raise CalibrationError("selection priority names an unknown sector")

    sector_size = min(_int(item["size"], "sector.size") for item in sectors)
    header = config.get("header")
    table = config.get("record_table")
    if not isinstance(header, dict) or not isinstance(table, dict):
        raise CalibrationError("header and record_table must be mappings")
    for section_name, section in (("header", header), ("record_table", table)):
        for key in ("name", "label", "description", "offset"):
            if key not in section:
                raise CalibrationError(f"{section_name} is missing {key}")
    header_offset = _int(header["offset"], "header.offset")
    header_size = _int(header.get("size"), "header.size")
    if header_offset < 0 or header_size <= 0 or header_offset + header_size > sector_size:
        raise CalibrationError("header lies outside a calibration sector")
    _validate_fields(header.get("fields"), header_size, "header")
    if selection["marker_field"] not in {field["name"] for field in header["fields"]}:
        raise CalibrationError("selection.marker_field is not a header field")

    table_offset = _int(table["offset"], "record_table.offset")
    record_size = _int(table.get("record_size"), "record_table.record_size")
    count = _int(table.get("count"), "record_table.count")
    if table_offset < 0 or record_size <= 0 or count <= 0:
        raise CalibrationError("record-table geometry is invalid")
    if table_offset + record_size * count > sector_size:
        raise CalibrationError("record table exceeds a calibration sector")
    records = table.get("records")
    if not isinstance(records, list) or len(records) != count:
        raise CalibrationError("record_table.records length must equal count")
    for index, record in enumerate(records):
        for key in ("name", "label"):
            if key not in record:
                raise CalibrationError(f"record {index} is missing {key}")
    _validate_fields(table.get("fields"), record_size, "record_table")

    unknown_ranges = config.get("unknown_ranges", [])
    if not isinstance(unknown_ranges, list):
        raise CalibrationError("unknown_ranges must be a list")
    for item in unknown_ranges:
        for key in ("name", "label", "description", "offset", "size"):
            if key not in item:
                raise CalibrationError(f"unknown range is missing {key}")
        offset = _int(item["offset"], f"{item['name']}.offset")
        size = _int(item["size"], f"{item['name']}.size")
        if offset < 0 or size <= 0 or offset + size > sector_size:
            raise CalibrationError(f"unknown range {item['name']} exceeds sector")
    return config


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    schema_path = Path(path) if path is not None else default_config_path()
    try:
        with schema_path.open("r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise CalibrationError(f"cannot load schema {schema_path}: {exc}") from exc
    return validate_config(config)


def _decode_field(data: bytes, base: int, field: Mapping[str, Any]) -> dict[str, Any]:
    offset = base + _int(field["offset"], f"{field['name']}.offset")
    size = _field_size(field)
    raw_bytes = data[offset:offset + size]
    if len(raw_bytes) != size:
        raise CalibrationError(f"field {field['name']} extends past sector")
    if field["type"] == "u8":
        raw: Any = raw_bytes[0]
    elif field["type"] == "u16le":
        raw = int.from_bytes(raw_bytes, "little")
    else:
        raw = list(raw_bytes)
    result = {
        "name": field["name"], "label": field["label"],
        "description": field["description"], "offset": f"0x{offset:04X}",
        "size": size, "raw_hex": raw_bytes.hex(), "raw": raw,
    }
    if field["type"] == "u16le":
        result["word_hex"] = f"0x{raw:04X}"
    if isinstance(raw, list):
        if "element_mask" in field:
            mask = _int(field["element_mask"], f"{field['name']}.element_mask")
            result["masked_values"] = [value & mask for value in raw]
        if "bias" in field or "scale" in field:
            bias, scale = field.get("bias", 0), field.get("scale", 1)
            result["decoded_values"] = [(value - bias) * scale for value in raw]
    else:
        value = raw
        if "mask" in field:
            mask = _int(field["mask"], f"{field['name']}.mask")
            result["masked"] = value & mask
            value &= mask
        if "bias" in field:
            result["unbiased"] = value - field["bias"]
            value -= field["bias"]
        if "scale" in field:
            result["scaled"] = value * field["scale"]
            value *= field["scale"]
        result["value"] = value
    if "unit" in field:
        result["unit"] = field["unit"]
    if "expected" in field:
        result["expected"] = field["expected"]
        result["matches_expected"] = raw == field["expected"]
    return result


def _range_summary(data: bytes, definition: Mapping[str, Any]) -> dict[str, Any]:
    offset, size = definition["offset"], definition["size"]
    value = data[offset:offset + size]
    return {
        "name": definition["name"], "label": definition["label"],
        "description": definition["description"],
        "offset": f"0x{offset:04X}", "size": size,
        "sha256": hashlib.sha256(value).hexdigest(),
        "zero_bytes": value.count(0), "erased_bytes": value.count(0xFF),
        "raw_hex": value.hex(),
    }


def decode_sector(data: bytes, config: Mapping[str, Any], role: str) -> dict[str, Any]:
    definition = next((item for item in config["sectors"] if item["name"] == role), None)
    if definition is None:
        raise CalibrationError(f"schema has no sector named {role}")
    if len(data) != definition["size"]:
        raise CalibrationError(f"{role} must be {definition['size']} bytes, got {len(data)}")
    header = config["header"]
    header_fields = [_decode_field(data, header["offset"], item)
                     for item in header["fields"]]
    marker_name = config["selection"]["marker_field"]
    marker = next(item for item in header_fields if item["name"] == marker_name)
    table = config["record_table"]
    records = []
    for index, record_definition in enumerate(table["records"]):
        base = table["offset"] + index * table["record_size"]
        records.append({
            **record_definition, "index": index, "offset": f"0x{base:04X}",
            "fields": [_decode_field(data, base, item) for item in table["fields"]],
        })
    return {
        "role": role, "description": definition["description"],
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "valid_marker": marker["raw"] == config["selection"]["valid_value"],
        "header": {key: header[key] for key in ("name", "label", "description", "offset", "size")}
                  | {"fields": header_fields},
        "record_table": {key: table[key] for key in
                         ("name", "label", "description", "offset", "record_size", "count")}
                        | {"records": records},
        "unknown_ranges": [_range_summary(data, item)
                           for item in config.get("unknown_ranges", [])],
    }


def decode_capture(data: bytes, config: Mapping[str, Any]) -> dict[str, Any]:
    read_start = config["transport"]["read_start"]
    decoded, raw_sectors = {}, {}
    for sector in config["sectors"]:
        start = sector["offset"] - read_start
        end = start + sector["size"]
        if start < len(data) < end:
            raise CalibrationError(
                f"capture ends inside {sector['name']} sector: "
                f"need {end} bytes, got {len(data)}")
        if len(data) >= end:
            value = data[start:end]
            raw_sectors[sector["name"]] = value
            decoded[sector["name"]] = decode_sector(value, config, sector["name"])
    if not decoded:
        minimum = min(item["offset"] - read_start + item["size"]
                      for item in config["sectors"])
        raise CalibrationError(f"capture is too short: need at least {minimum} bytes")
    priority = config["selection"]["priority"]
    selected = next((name for name in priority
                     if name in decoded and decoded[name]["valid_marker"]),
                    None)
    if selected is None and all(name in decoded for name in priority):
        selected = config["selection"]["fallback"]
    identical = None
    if len(priority) >= 2 and all(name in raw_sectors for name in priority[:2]):
        identical = raw_sectors[priority[0]] == raw_sectors[priority[1]]
    return {
        "schema": {"schema_version": config["schema_version"],
                   "radio": config.get("radio"),
                   "firmware_generation": config.get("firmware_generation")},
        "capture_size": len(data), "capture_sha256": hashlib.sha256(data).hexdigest(),
        "decoded_sectors": decoded,
        "sector_order": [item["name"] for item in config["sectors"]
                         if item["name"] in decoded],
        "primary_mirror_identical": identical,
        "startup_selection": selected, "notes": config.get("notes", {}),
    }


def _format_report_value(value: Any) -> str:
    if isinstance(value, float):
        return format(value, ".15g")
    if isinstance(value, list):
        return "[" + ", ".join(_format_report_value(item) for item in value) + "]"
    return str(value)


def _format_field(field: Mapping[str, Any]) -> list[str]:
    extra_fields = (
        ("word", "word_hex"), ("value", "value"), ("masked", "masked"),
        ("unbiased", "unbiased"), ("scaled", "scaled"),
        ("decoded_values", "decoded_values"), ("masked_values", "masked_values"),
        ("unit", "unit"), ("expected", "expected"),
        ("matches_expected", "matches_expected"),
    )
    extras = [f"{label}={_format_report_value(field[key])}"
              for label, key in extra_fields if key in field]
    suffix = f" ({', '.join(extras)})" if extras else ""
    return [f"  {field['offset']} {field['label']} [{field['name']}]: "
            f"raw={field['raw_hex']}{suffix}", f"    {field['description']}"]


def _format_sector(sector: Mapping[str, Any], unknown_hex: bool) -> list[str]:
    lines = [f"[{sector['role']}] {sector['description']}",
             f"size={sector['size']} sha256={sector['sha256']} "
             f"valid_marker={sector['valid_marker']}",
             f"{sector['header']['label']}: {sector['header']['description']}"]
    for field in sector["header"]["fields"]:
        lines.extend(_format_field(field))
    table = sector["record_table"]
    lines.extend(["", f"{table['label']}: {table['description']}"])
    for record in table["records"]:
        metadata = [f"{key}={value}" for key, value in record.items()
                    if key not in {"name", "label", "index", "offset", "fields"}]
        suffix = f" ({', '.join(metadata)})" if metadata else ""
        lines.extend(["", f"record {record['index']:02d} {record['label']} "
                      f"[{record['name']}] offset={record['offset']}{suffix}"])
        for field in record["fields"]:
            lines.extend(_format_field(field))
    lines.extend(["", "Unknown/reserved ranges (preserved but not decoded):"])
    for item in sector["unknown_ranges"]:
        lines.extend([
            f"  {item['offset']} {item['label']} [{item['name']}]: size={item['size']} "
            f"sha256={item['sha256']} zero={item['zero_bytes']} ff={item['erased_bytes']}",
            f"    {item['description']}"])
        if unknown_hex:
            lines.append(f"    raw_hex={item['raw_hex']}")
    return lines


def format_report(report: Mapping[str, Any], unknown_hex: bool = False) -> str:
    schema = report["schema"]
    startup_selection = report["startup_selection"]
    if startup_selection is None:
        startup_selection = "unknown (not all selection sectors were captured)"
    lines = [f"{schema['radio']} calibration report",
             f"schema_version={schema['schema_version']} "
             f"firmware_generation={schema['firmware_generation']}",
             f"capture_size={report['capture_size']} capture_sha256={report['capture_sha256']}",
             f"primary_mirror_identical={report['primary_mirror_identical']}",
             f"OEM_startup_selection={startup_selection}"]
    lines.extend(f"note.{name}: {note}" for name, note in report["notes"].items())
    for index, role in enumerate(report["sector_order"]):
        sector = report["decoded_sectors"][role]
        lines.append("")
        if index == 1 and report["primary_mirror_identical"]:
            lines.extend([f"[{role}] byte-identical to first sector; constants not repeated",
                          f"size={sector['size']} sha256={sector['sha256']} "
                          f"valid_marker={sector['valid_marker']}"])
        else:
            lines.extend(_format_sector(sector, unknown_hex))
    return "\n".join(lines) + "\n"


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _read_exact(pipe: Any, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = pipe.read(size - len(value))
        if not chunk:
            break
        value.extend(chunk)
    return bytes(value)


def _enter_read_mode(pipe: Any) -> None:
    previous = pipe.timeout
    pipe.timeout = min(float(previous or 2.0), 0.25)
    try:
        for _ in range(5):
            pipe.write(ENTER_MAGIC)
            if _read_exact(pipe, 1) == ACK:
                return
        raise CalibrationError("radio did not acknowledge read mode after five attempts")
    finally:
        pipe.timeout = previous


def _read_block(pipe: Any, block: int, size: int) -> bytes:
    if not 0 <= block <= 0xFFFF:
        raise CalibrationError(f"block address out of range: {block}")
    command = bytes([READ_OPCODE, block >> 8, block & 0xFF])
    pipe.write(command + bytes([_checksum(command)]))
    reply = _read_exact(pipe, size + 4)
    if len(reply) != size + 4:
        raise CalibrationError(f"short reply for block 0x{block:04X}: got {len(reply)}")
    if reply[:3] != command or reply[-1] != _checksum(reply[:-1]):
        raise CalibrationError(f"invalid reply for block 0x{block:04X}")
    return reply[3:-1]


def read_live_calibration(pipe: Any, config: Mapping[str, Any],
                          progress: Callable[[int, int], None] | None = None) -> bytes:
    transport = config["transport"]
    size = transport["block_size"]
    start = transport["read_start"] // size
    count = transport["read_size"] // size
    entered = False
    try:
        _enter_read_mode(pipe)
        entered = True
        values = []
        for index in range(count):
            values.append(_read_block(pipe, start + index, size))
            if progress:
                progress(index + 1, count)
        return b"".join(values)
    finally:
        if entered:
            pipe.write(EXIT_MAGIC)


def _serial_factory(port: str, timeout: float) -> Any:
    try:
        import serial
    except ImportError as exc:
        raise CalibrationError("pyserial is required for live reads") from exc
    return serial.Serial(port=port, baudrate=115200, timeout=timeout,
                         write_timeout=timeout)


def _render(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.json:
        clean = json.loads(json.dumps(report))
        if not args.unknown_hex:
            for sector in clean["decoded_sectors"].values():
                for item in sector["unknown_ranges"]:
                    item.pop("raw_hex", None)
        print(json.dumps(clean, indent=2))
    else:
        print(format_report(report, args.unknown_hex), end="")


def _decode_command(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    print(f"schema={Path(args.config).resolve() if args.config else default_config_path().resolve()}",
          file=sys.stderr)
    _render(decode_capture(args.image.read_bytes(), config), args)


def _read_command(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    print(f"schema={Path(args.config).resolve() if args.config else default_config_path().resolve()}",
          file=sys.stderr)
    port = args.port or detect_port()
    output = next_cache_path(default_cache_dir()).resolve()
    part = Path(str(output) + ".part")
    print(f"port={port}", file=sys.stderr)
    pipe = _serial_factory(port, args.timeout)
    try:
        data = read_live_calibration(pipe, config, lambda done, total: print(
            f"read block {done}/{total}", file=sys.stderr, flush=True))
    finally:
        pipe.close()
    try:
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        part.replace(output)
    except Exception:
        if part.exists():
            part.unlink()
        raise
    print(f"saved={output}\nbytes={len(data)}\nsha256={hashlib.sha256(data).hexdigest()}",
          file=sys.stderr)
    _render(decode_capture(data, config), args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rt4d-calibration",
        description="Schema-driven read-only RT-4D / DM-UV4R calibration tool")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path,
                        help="YAML schema; clone launcher defaults to .config/rt4d_oem_v325.yaml")
    common.add_argument("--json", action="store_true")
    common.add_argument("--unknown-hex", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    decode = subparsers.add_parser("decode", parents=[common],
                                   help="decode an existing capture")
    decode.add_argument("image", type=Path)
    decode.set_defaults(handler=_decode_command)
    read = subparsers.add_parser("read", parents=[common],
                                 help="read and decode configured sectors")
    read.add_argument("--port",
                      help="serial port; auto-detected when exactly one candidate exists")
    read.add_argument("--timeout", type=float, default=2.0)
    read.set_defaults(handler=_read_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (CalibrationError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
