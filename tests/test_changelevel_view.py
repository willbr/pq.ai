"""A changelevel must not lose the window size. _load_map rebuilds the
renderer and used to reset _view_wh to (0, 0); frontends that only call
resize() when the WINDOW size changes (win_gdi, mac_cocoa) then never re-sync,
so every overlay laid out from w/h collapsed to 0x0: the bottom status bar
moved to y=-8 (off-screen above the window) and the crosshair to the top-left
corner. The tk frontend masked it by calling resize() every tick."""

import _bootstrap  # noqa: F401

from client import Client, InputState

W, H = 800, 600


def _statusbar(rf):
    sw = [o for o in rf.overlays if o[4] == "sw"]
    assert sw, "status bar overlay missing"
    return sw[0]


def test_view_size_survives_changelevel():
    c = Client("e1m1")
    c.resize(W, H)
    c.set_video_res((240, 160))   # <320 wide: keep the text status bar overlay
    rf = c.frame(0.05, InputState())
    assert _statusbar(rf)[1] == H - 8
    assert rf.crosshair == (W // 2, H // 2)
    fb0 = rf.framebuffer
    assert fb0 is not None

    # slipgate: trigger_changelevel queued a map swap; no resize() in between
    c.sv.changelevel = "e1m2"
    rf = c.frame(0.05, InputState())
    assert c.mapname == "e1m2"
    assert _statusbar(rf)[1] == H - 8, "status bar lost the window height"
    assert rf.crosshair == (W // 2, H // 2), "crosshair lost the window size"
    # the rebuilt renderer renders at the window-derived resolution, not its
    # construction default
    fb, fw, fh = rf.framebuffer
    assert (fw, fh) == (fb0[1], fb0[2]), "renderer lost the window size"


def test_console_map_command_keeps_view_size():
    c = Client("e1m1")
    c.resize(W, H)
    c.set_video_res((240, 160))   # <320 wide: keep the text status bar overlay
    c.frame(0.05, InputState())
    c.con.execute("map e1m2")
    rf = c.frame(0.05, InputState())
    assert c.mapname == "e1m2"
    assert _statusbar(rf)[1] == H - 8
    assert rf.crosshair == (W // 2, H // 2)


if __name__ == "__main__":
    test_view_size_survives_changelevel()
    test_console_map_command_keeps_view_size()
    print("OK")
