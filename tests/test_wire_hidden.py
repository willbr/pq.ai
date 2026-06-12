"""Hidden-line wireframe (the `wire_hidden` cvar).

Edge-only wireframe is an X-ray view: it draws every PVS-visible face's edges as
lines with no hidden-surface removal, so you see through walls. With `wire_hidden`
set, the Client renders wireframe mode through the same back-to-front (painter's)
polygon path the flat-shaded mode uses -- so near faces occlude far ones -- and
tags the frame "wire_hidden" for the frontend to paint as background-filled,
green-outlined polygons. Boots the real shareware stack, like the other client
tests (so it needs quake-shareware/id1/pak0.pak).
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from client import Client, InputState


def _wire(c):
    """Put the client in plain wireframe mode (flat + zbuf off)."""
    c.flat = False
    c.zbuf = False
    c._apply_mode()
    assert c.mode == "wire"


def test_default_wire_is_xray_segments():
    c = Client("e1m1")
    c.resize(640, 480)
    _wire(c)
    assert c.wire_hidden is False                 # off by default
    rf = c.frame(0.016, InputState())
    assert rf.mode == "wire"
    assert rf.segs and len(rf.segs) > 0           # edge-only line segments
    assert rf.polys is None


def test_wire_hidden_uses_occluded_polys():
    c = Client("e1m1")
    c.resize(640, 480)
    _wire(c)
    c.con.execute("wire_hidden 1")                # cvar toggles it on
    assert c.wire_hidden is True
    rf = c.frame(0.016, InputState())
    assert rf.mode == "wire_hidden"               # distinct mode for the frontend
    assert rf.polys and len(rf.polys) > 0         # back-to-front filled polygons
    assert rf.segs is None


def test_wire_hidden_leaves_flat_and_zbuf_untouched():
    c = Client("e1m1")
    c.resize(640, 480)
    c.con.execute("wire_hidden 1")
    c.flat = True
    c.zbuf = False
    c._apply_mode()
    assert c.frame(0.016, InputState()).mode == "flat"
    c.zbuf = True
    c._apply_mode()
    assert c.frame(0.016, InputState()).mode == "zbuf"


def test_cvar_round_trip():
    c = Client("e1m1")
    c.con.execute("wire_hidden 1")
    assert c.wire_hidden is True
    c.con.execute("wire_hidden 0")
    assert c.wire_hidden is False


def test_all():
    test_default_wire_is_xray_segments()
    test_wire_hidden_uses_occluded_polys()
    test_wire_hidden_leaves_flat_and_zbuf_untouched()
    test_cvar_round_trip()


if __name__ == "__main__":
    test_all()
    print("OK")
