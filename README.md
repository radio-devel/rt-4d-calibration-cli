# RT-4D Calibration CLI

A standalone, schema-driven, read-only calibration dumper and decoder for the iRadio RT-4D /
DM-UV4R OEM V3.25 generation.

It decodes the known 4 KiB main-MCU/BK4819 calibration sector, compares it
with the 4 KiB firmware-maintained mirror, and prints every known global and
per-band field with its flash offset and raw value. Unknown bytes are preserved
and reported by hash. Use `--unknown-hex` to print them verbatim.

The separate 23-byte DMR/baseband tuning record is **not** claimed to be in
these sectors: its persistent address has not been proven.

The default layout is `.config/rt4d_oem_v325.yaml`. Sector offsets and sizes,
header boundaries, record geometry, field names, labels, descriptions, data
types, masks, biases, scales, units, band names, and unknown ranges all come
from this YAML file rather than Python constants.

## Validation status

Offline status: the decoder, schema validation, reporting, and serial framing
pass automated tests using synthetic captures and a fake serial transport.

Live-radio status: **verified for two reads from two physical radios on
2026-09-13**. Automatic detection selected a Prolific USB adapter, and all eight
block replies in each run passed echoed-header and checksum validation. Each
capture had valid `0x9A` markers and byte-identical primary and mirror sectors;
decoded values and padding had plausible internal relationships. The two
captures are distinct and remain private and ignored by Git. This establishes
successful reading and plausible decoding on those two units. It does not
establish RF calibration accuracy, compatibility with other radios or firmware
generations, or broad reliability. Automated tests remain offline evidence only.

## Run from a source checkout

The top-level launcher automatically loads `.config/rt4d_oem_v325.yaml`
relative to its own location, independent of the current working directory.

```sh
cd rt-4d-calibration-cli
python3 -m pip install -r requirements.txt
./rt4d-calibration --help
```

## Optional editable install

Python 3.9 or newer is required. A live read additionally requires pyserial.

```sh
cd rt-4d-calibration-cli
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Decode an existing backup

The input may be one 4096-byte sector, an 8192-byte primary-plus-mirror backup,
or a larger raw flash image. For a larger image, only offsets
`0x000000..0x001FFF` are decoded. An input ending partway through a sector is
rejected instead of silently discarding that partial sector. When a lone
primary sector has an invalid marker, startup selection is reported as unknown
because an uncaptured mirror could still be valid.

```sh
./rt4d-calibration decode RADIO_calibration.bin
./rt4d-calibration decode RADIO_calibration.bin --json
./rt4d-calibration decode RADIO_calibration.bin --unknown-hex
```

JSON output retains all known raw and decoded values. Raw hex for the two
unknown ranges is omitted unless `--unknown-hex` is supplied; their sizes,
SHA-256 hashes, zero counts, and erased-byte counts are always included.

## Read a radio

**The radio must be powered on and booted into OEM firmware before running this
command.** The supplied schema and live-read workflow target the OEM V3.25
generation. Do not run the command while the radio is in its resident bootloader
or running experimental firmware.

The live `read` command has completed the two verified reads described above and
remains experimental.

Only connect to a radio you own or are authorized to access. Confirm the radio's
identity before starting a read and keep the resulting calibration dump private;
it may contain unit-specific data. After an interrupted or uncertain programming
attempt, power-cycle the radio before reading: OEM V3.25 does not visibly clear
its pending write state merely by entering programming mode.

```sh
./rt4d-calibration read
```

The tool first checks `/dev/serial/by-id`. If exactly one persistent serial
device is present, it is selected automatically. Otherwise it considers
`ttyUSB*` and `ttyACM*` devices reported by pyserial. It never guesses when
multiple candidates exist; select one explicitly in that case:

```sh
./rt4d-calibration read --port /dev/serial/by-id/EXACT_DEVICE
```

This detects the USB serial adapter, not the physical radio behind the cable.
The clone-local `.cache/` directory is created automatically. Captures receive
collision-resistant timestamped names such as
`rt4d-calibration-20260913-123456-123456+0200.bin`; there is no output-path
argument.

With the supplied schema, the command is designed to read exactly eight
1024-byte blocks: primary sector `0x000000..0x000FFF` and mirror
`0x001000..0x001FFF`. It is implemented to send only:

- programming-mode entry `34 52 05 10 9b`;
- opcode `0x52` block reads; and
- programming-mode exit `34 52 05 ee 79`.

There is no calibration writer or other external-flash write opcode in this
tool. The backup is written atomically through an adjacent mode-0600 `.part`
file. Prefer two independent reads, then compare both the full SHA-256
and primary/mirror status. Keep all unit-specific files outside public source
repositories.

## Alternate schemas

Copy the default schema, edit it, and select it explicitly:

```sh
./rt4d-calibration decode capture.bin --config .config/alternate.yaml
```

The schema is validated before a capture or radio is read. Fields exceeding
their declared header or record boundaries, records exceeding a sector, and
live ranges that are not protocol-block-aligned are rejected.

## Output interpretation

- A marker of `0x9A` is the only sector-validity check found in the OEM V3.25
  startup path. It is not a checksum of the sector.
- Frequency correction is printed both as the biased stored word and as signed
  10 Hz counts / Hz.
- FM deviation words are printed in full and with their used low 12 bits.
- CTCSS and DCS deviations are printed raw and masked to the used low 7 bits.
- PA-bias table entries are hardware control codes, not watts or ERP.
- Packed volume/DAC words remain packed because their internal bit allocation
  is not established strongly enough to label further.
- Bands 11 through 14 are present in the tuning format but are marked as not
  selectable by the audited OEM V3.25 application.

## Tests

Tests use synthetic sectors and a fake serial transport. They never access a
radio.

```sh
python3 -m pip install -r requirements-dev.txt
PYTHONPATH=src python3 -m pytest
```

## License

This project is licensed under the GNU General Public License, version 3 or
any later version (`GPL-3.0-or-later`). See `LICENSE` and `ATTRIBUTION.md`.
