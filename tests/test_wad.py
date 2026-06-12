"""Tests for the WAD2 parser (gfx.wad inside pak0.pak): directory, qpic
lumps, raw lumps, case-insensitive lookup. Needs the shareware pak."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.pak import Pak
from quake.wad import Wad


def _wad():
    return Wad(Pak("quake-shareware/id1/pak0.pak").read("gfx.wad"))


def test_directory_parses():
    w = _wad()
    names = w.names()
    assert len(names) == 163
    assert "sbar" in names and "num_0" in names and "conchars" in names


def test_qpic_sizes_match_sbar_layout():
    w = _wad()
    for name, (ww, hh) in (("sbar", (320, 24)), ("ibar", (320, 24)),
                           ("num_0", (24, 24)), ("anum_minus", (24, 24)),
                           ("face1", (24, 24)), ("sb_sigil1", (8, 16)),
                           ("inv_lightng", (48, 16)), ("backtile", (64, 64))):
        pw, ph, px = w.qpic(name)
        assert (pw, ph) == (ww, hh), name
        assert len(px) == ww * hh, name


def test_lookup_is_case_insensitive():
    w = _wad()
    assert w.qpic("SBAR") == w.qpic("sbar")


def test_raw_lump_conchars():
    # CONCHARS is type 0x44 (raw 128x128 font sheet, no qpic header)
    w = _wad()
    assert len(w.lump("conchars")) == 128 * 128


def test_qpic_rejects_non_qpic_lump():
    w = _wad()
    try:
        w.qpic("conchars")
    except ValueError:
        pass
    else:
        assert False, "qpic() accepted a non-qpic lump"


if __name__ == "__main__":
    test_directory_parses()
    test_qpic_sizes_match_sbar_layout()
    test_lookup_is_case_insensitive()
    test_raw_lump_conchars()
    test_qpic_rejects_non_qpic_lump()
    print("OK")
