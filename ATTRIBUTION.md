# Attribution and provenance

This project is licensed under the GNU General Public License, version 3 or
any later version (`GPL-3.0-or-later`).

The iRadio programming-mode entry/exit behavior and opcode `0x52` external
flash read protocol were implemented with reference to the CHIRP project,
including `chirp.drivers.iradio_common`, the experimental
`chirp.drivers.iradio_dmuv4r` driver, and the local read-only raw-flash helper.
Those sources are distributed under GNU GPL terms. CHIRP contributors retain
copyright in their respective work, including Jim Unroe's copyright notice on
`iradio_common.py`.

The calibration geometry, field meanings, confidence boundaries, and safety
notes were derived from clean-room RT-4D / DM-UV4R analysis. The experimental
`dmuv4r-oefw` firmware is Apache-2.0 licensed; Apache-2.0 material is compatible
for inclusion in a GPLv3 work. Vendor firmware and tuning applications were
used as behavioral evidence only and are not redistributed by this project.

PyYAML and pyserial are external runtime dependencies and retain their own
licenses. They are not vendored here.
