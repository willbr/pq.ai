"""Integration test: in zbuf (textured) mode the Client composites centerprint,
console, and menu into the framebuffer with the conchars font instead of
emitting them as OS-native overlays; non-zbuf modes keep the overlay path.

Boots the full engine stack -- needs quake-shareware/id1/pak0.pak. Each check
renders the SAME frame twice with dt=0 (so sv.time and the scene are identical)
toggling one UI element, and asserts the framebuffer changed -- proof the
element was composited -- while the RenderFrame carries no OS overlay for it."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from client import Client, InputState
from quake.sbar import SBAR_LINES


def _client():
    c = Client("e1m1")
    c.resize(640, 480)
    c.frame(0.0, InputState())              # settle one frame
    return c


def test_centerprint_composited_not_overlaid_in_zbuf():
    c = _client()
    assert c.mode == "zbuf"
    c.sv.center_msg = None
    base = bytes(c.frame(0.0, InputState()).framebuffer[0])
    c.sv.center_msg = ("HELLO QUAKE", c.sv.time)
    rf = c.frame(0.0, InputState())
    # framebuffer changed -> centerprint was drawn into it
    assert bytes(rf.framebuffer[0]) != base
    # ...and no OS-native center overlay was emitted
    assert all(o[4] != "center" for o in rf.overlays)


def test_centerprint_overlaid_in_flat_mode():
    c = _client()
    # from the default both zbuf+flat are on (zbuf wins); toggling zbuf off
    # drops into flat mode, where the overlay path still applies.
    c.frame(0.0, InputState(commands=frozenset({"zbuf"})))
    assert c.mode == "flat"
    c.sv.center_msg = ("HELLO QUAKE", c.sv.time)
    rf = c.frame(0.016, InputState())
    assert any(o[4] == "center" and "HELLO" in o[2] for o in rf.overlays)


def test_console_composited_not_overlaid_in_zbuf():
    c = _client()
    base = bytes(c.frame(0.0, InputState()).framebuffer[0])
    c.con.active = True
    c.con.print("test console line")
    rf = c.frame(0.0, InputState())
    assert rf.console is None               # not handed to the frontend
    assert bytes(rf.framebuffer[0]) != base  # drawn into the framebuffer


def test_menu_composited_not_overlaid_in_zbuf():
    c = _client()
    base = bytes(c.frame(0.0, InputState()).framebuffer[0])
    c.menu.active = True
    rf = c.frame(0.0, InputState())
    assert rf.menu is None
    assert bytes(rf.framebuffer[0]) != base


def test_intermission_block_shared_between_paths():
    c = _client()
    c.sv.gset_f("intermission_running", 1.0)
    c.sv.intermission_time = 83.0                       # 1:23
    c.sv.gset_f("found_secrets", 2.0); c.sv.gset_f("total_secrets", 4.0)
    c.sv.gset_f("killed_monsters", 15.0); c.sv.gset_f("total_monsters", 30.0)
    c.intermission = True
    ist = c.sv.intermission_stats()
    block = c._intermission_block(ist)
    assert "Time      1:23" in block
    assert "Secrets   2 / 4" in block and "Kills     15 / 30" in block
    # the wire/flat overlay path emits this text block; zbuf now draws the
    # authentic Sbar_IntermissionOverlay pics instead (see test_sbar.py).
    # exercise the zbuf path once (it now draws the Sbar intermission pics; see
    # test_sbar.py and test_intermission_pics_in_zbuf for the pixel assertions)
    c.frame(0.0, InputState())
    # the flat path emits the identical string as an overlay.
    rf = c.frame(0.05, InputState(commands=frozenset({"zbuf"})))
    panel = [o for o in rf.overlays if o[4] == "center"]
    assert panel and panel[0][2] == block


def test_intermission_pics_in_zbuf():
    c = _client()                                   # 320x200 framebuffer, fh=200
    base = bytes(c.frame(0.0, InputState()).framebuffer[0])
    c.sv.gset_f("intermission_running", 1.0)
    c.sv.intermission_time = 83.0
    c.sv.gset_f("found_secrets", 2.0); c.sv.gset_f("total_secrets", 4.0)
    c.sv.gset_f("killed_monsters", 15.0); c.sv.gset_f("total_monsters", 30.0)
    c.intermission = True
    rf = c.frame(0.0, InputState())
    assert bytes(rf.framebuffer[0]) != base             # something was drawn
    assert all(o[4] != "center" for o in rf.overlays)   # no OS overlay in zbuf
    # the pic path (not the conchars text fallback) ran: the 'complete' title
    # pic landed at id's (64,24), centred by (fbw-320)//2.
    fb, fw, fh = rf.framebuffer
    assert fh >= 200, f"expected full-height fb, got {fh}"
    sx = (fw - 320) // 2
    pw, ph, px = c.sb_complete
    hits = total = 0
    for r in range(ph):
        for cc in range(pw):
            s = px[r * pw + cc]
            if s != 255:
                total += 1
                if fb[(24 + r) * fw + sx + 64 + cc] == s:
                    hits += 1
    assert total and hits > total * 0.5, f"complete title pic not drawn ({hits}/{total})"


def test_intermission_hides_status_bar_and_fills_3d():
    # the level-complete overlay owns the whole screen: the sprite status bar is
    # hidden (sbar_lines 0, so the 3D view renders full height with no shrunk
    # band) and the text status string isn't emitted either.
    c = _client()
    assert c.rend.sbar_lines == SBAR_LINES      # normal play: bar shrinks the view
    c.sv.gset_f("intermission_running", 1.0)
    c.sv.intermission_time = 83.0
    c.sv.gset_f("found_secrets", 2.0); c.sv.gset_f("total_secrets", 4.0)
    c.sv.gset_f("killed_monsters", 15.0); c.sv.gset_f("total_monsters", 30.0)
    c.intermission = True
    rf = c.frame(0.0, InputState())
    assert c.rend.sbar_lines == 0               # bar hidden -> full-height 3D
    assert all(o[4] != "sw" for o in rf.overlays)   # no bottom status string


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")
