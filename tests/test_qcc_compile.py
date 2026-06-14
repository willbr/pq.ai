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


V101 = "quake-source/quake-tools/qcc/v101qc/progs.src"


def _lumps(data):
    import struct
    h = struct.unpack_from("<2i 12i i", data, 0)
    names = ["statements", "globaldefs", "fielddefs", "functions",
             "strings", "globals"]
    esz = {"statements": 8, "globaldefs": 8, "fielddefs": 8,
           "functions": 36, "strings": 1, "globals": 4}
    out = {}
    for i, n in enumerate(names):
        ofs, cnt = h[2 + i * 2], h[3 + i * 2]
        out[n] = data[ofs:ofs + cnt * esz[n]]
    out["entityfields"] = h[14]
    out["crc"] = h[1]
    return out


def test_per_lump_matches_oracle():
    mine = _lumps(compile_progs_src(V101))
    ref = _lumps(_oracle())
    assert mine["crc"] == ref["crc"], "crc"
    assert mine["entityfields"] == ref["entityfields"], "entityfields"
    for lump in ("strings", "functions", "statements", "globaldefs",
                 "fielddefs", "globals"):
        assert mine[lump] == ref[lump], (
            f"lump {lump} differs: mine={len(mine[lump])} ref={len(ref[lump])}")


def test_byte_identical_to_oracle():
    assert compile_progs_src(V101) == _oracle()


def test_self_compiled_boots():
    from quake.pak import Pak
    from quake.bsp import Bsp
    from quake.progs import Progs
    from quake.sv import Server
    from quake.physics import Physics
    data = compile_progs_src(V101)
    pak = Pak("quake-shareware/id1/pak0.pak")
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(data), bsp=b, mapname="maps/e1m1.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    for _ in range(3):
        sv.run_frame(0.1)


if __name__ == "__main__":
    test_oracle_present_and_valid()
    test_inline_minimal_compile()
    test_crc_matches_oracle()
    test_per_lump_matches_oracle()
    test_byte_identical_to_oracle()
    test_self_compiled_boots()
    print("OK")
