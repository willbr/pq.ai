"""Byte-identity tests for quake/qcc against id's qccdos.exe oracle (v101qc).
See docs/superpowers/specs/2026-06-14-qcc-python-port-design.md."""
import _bootstrap  # noqa: F401
import struct
import os
import tempfile

from quake.qcc import compile_progs_src
from quake.progs import Progs

ORACLE = "tests/data/progs_v101_oracle.dat"


def _oracle():
    with open(ORACLE, "rb") as f:
        return f.read()


def test_oracle_present_and_valid():
    data = _oracle()
    assert len(data) == 410616, len(data)
    ver, crc = struct.unpack_from("<ii", data, 0)
    assert ver == 6 and crc == 5927, (ver, crc)


def test_inline_minimal_compile():
    # a tiny self-contained progs: one field, one builtin, one function
    src = """
.float health;
void(string s) dprint = #1;
float() main =
{
    local float x;
    x = 3 + 4;
    dprint("hi");
    return;
};
"""
    d = tempfile.mkdtemp(prefix="qccmini")
    with open(f"{d}/test.qc", "w") as f:
        f.write(src)
    with open(f"{d}/progs.src", "w") as f:
        f.write("progs.dat\ntest.qc\n")
    data = compile_progs_src(f"{d}/progs.src")
    p = Progs(data)                              # our loader accepts it
    names = {fn.name for fn in p.functions if fn}
    assert "main" in names and "dprint" in names
    assert p.functions and any(fn and fn.builtin == 1 for fn in p.functions)


def test_crc_matches_oracle():
    # compiling the real v101qc must reproduce id's progdefs CRC (5927)
    data = compile_progs_src(
        "quake-source/quake-tools/qcc/v101qc/progs.src")
    _, crc = struct.unpack_from("<ii", data, 0)
    assert crc == 5927, crc


if __name__ == "__main__":
    test_oracle_present_and_valid()
    test_inline_minimal_compile()
    test_crc_matches_oracle()
    print("OK")
