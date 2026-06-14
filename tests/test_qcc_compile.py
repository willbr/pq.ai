"""Byte-identity tests for quake/qcc against id's qccdos.exe oracle (v101qc).
See docs/superpowers/specs/2026-06-14-qcc-python-port-design.md."""
import _bootstrap  # noqa: F401
import struct

ORACLE = "tests/data/progs_v101_oracle.dat"


def _oracle():
    with open(ORACLE, "rb") as f:
        return f.read()


def test_oracle_present_and_valid():
    data = _oracle()
    assert len(data) == 410616, len(data)
    ver, crc = struct.unpack_from("<ii", data, 0)
    assert ver == 6 and crc == 5927, (ver, crc)


if __name__ == "__main__":
    test_oracle_present_and_valid()
    print("OK")
