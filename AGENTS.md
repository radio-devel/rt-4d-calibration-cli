# Repository Guidelines

## Project Structure & Module Organization

The clone-local `rt4d-calibration` launcher resolves repository-relative
configuration and imports the package from `src/rt4d_calibration_cli/`.
`cli.py` owns schema validation, decoding, serial-port selection, read-only
transport, reporting, and cache storage. Keep calibration layouts in
`.config/*.yaml`; offsets, boundaries, names, descriptions, and transforms
must not be embedded in Python. Tests live in `tests/`. Licensing and source
boundaries are documented in `LICENSE` and `ATTRIBUTION.md`.

Generated captures belong in ignored `.cache/`. Never commit `.ssh/`, private
keys, radio dumps, calibration values, serial numbers, or `.part` files.

## Build, Test, and Development Commands

```sh
python3 -m pip install -r requirements.txt
./rt4d-calibration --help
./rt4d-calibration decode capture.bin
```

Install `requirements-dev.txt` for development, then run:

```sh
PYTHONPATH=src python3 -m pytest -q
```

There is no build step. Test decoding with existing or synthetic captures;
do not contact a radio as part of ordinary development.

## Coding Style & Naming Conventions

Use four-space Python indentation, type annotations, `pathlib.Path`, and
descriptive `snake_case` names. Keep functions focused and fail closed on
ambiguous ports, invalid schemas, truncated replies, and checksum failures.
Use YAML `snake_case` identifiers with human-readable `label` and concise
`description` properties. Every source or schema file must retain the
`SPDX-License-Identifier: GPL-3.0-or-later` header.

## Testing Guidelines

Tests use pytest and must be named `test_*`. Cover schema boundary validation,
raw and transformed values, primary/mirror behavior, clone-relative paths,
and exact wire frames. Serial tests must use `FakePipe`; automated tests must
never open `/dev/ttyUSB*` or `/dev/serial/by-id/*`. Add a regression test with
every behavior change.

## Commit & Pull Request Guidelines

This standalone directory has no repository-local Git history yet. Use short,
imperative commit subjects such as `Add schema validation for array fields`.
Pull requests should explain the evidence for calibration-map changes, list
tests run, and identify compatibility or privacy implications. Include console
output when useful; never attach private captures or keys.

## Live-Radio Safety

Live serial access requires explicit current authorization and an identified
radio. Preserve the read-only design: programming-mode entry, opcode `0x52`
reads, and exit only. Do not add write, erase, reset, calibration-adjustment,
or RF-transmit commands without a separately reviewed scope.
