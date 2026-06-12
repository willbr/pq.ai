"""Tests for the VGA pixel-aspect toggle: yfocal in the zbuf renderer
(R_ViewChanged's yscale = xscale * pixelAspect), client plumbing (cvar,
menu, RenderFrame), and that wire/flat stay square. Boots the shareware
stack like the other client tests."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import client

CRT = 5.0 / 6.0


def _capture_zbuf_args(c):
    """Run frames, capture render_zbuffer's args so the same instant can be
    re-rendered with different renderer settings (lightstyles animate with
    sv.time, so two frame() calls are never pixel-comparable)."""
    captured = {}
    orig = c.rend.render_zbuffer
    def spy(*a, **kw):
        captured["a"], captured["kw"] = list(a), dict(kw)
        return orig(*a, **kw)
    c.rend.render_zbuffer = spy
    c.frame(0.016, client.InputState())
    c.rend.render_zbuffer = orig
    return captured["a"], captured["kw"]


def _gun_rows(rend, a, kw):
    """Diff a render with and without the view model: the rows the gun
    occupies. view_model is positional arg 5 or the kw."""
    (fb1, w, h), _ = rend.render_zbuffer(*a, **kw)
    if "view_model" in kw:
        kw2 = dict(kw, view_model=None); a2 = a
    else:
        a2 = list(a); a2[5] = None; kw2 = kw
    (fb2, _w, _h), _ = rend.render_zbuffer(*a2, **kw2)
    fb1, fb2 = bytes(fb1), bytes(fb2)
    return [y for y in range(h) if fb1[y*w:(y+1)*w] != fb2[y*w:(y+1)*w]]


def test_crt_aspect_widens_vertical_fov():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "zbuf"
    c.set_video_res((320, 200))
    a, kw = _capture_zbuf_args(c)
    rows_square = _gun_rows(c.rend, a, kw)
    c.rend.pixel_aspect = CRT
    rows_crt = _gun_rows(c.rend, a, kw)
    c.rend.pixel_aspect = 1.0
    # wider vertical FOV -> more of the gun (which hangs off the bottom
    # edge) survives the bottom clip
    assert rows_crt and rows_square
    assert min(rows_crt) < min(rows_square)
    assert len(rows_crt) > len(rows_square)


def test_wire_mode_ignores_pixel_aspect():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "wire"
    c.frame(0.016, client.InputState())
    eye = (c.pos[0], c.pos[1], c.pos[2] + 22)
    segs1, _ = c.rend.render(eye, c.yaw, c.pitch)
    c.rend.pixel_aspect = CRT
    segs2, _ = c.rend.render(eye, c.yaw, c.pitch)
    c.rend.pixel_aspect = 1.0
    assert segs1 == segs2


def test_renderframe_reports_pixel_aspect():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "zbuf"
    c.set_video_res((320, 200))
    rf = c.frame(0.016, client.InputState())
    assert rf.pixel_aspect == 1.0                  # default: square
    c.con.execute("pixel_aspect 0.8333333")
    rf = c.frame(0.016, client.InputState())
    assert abs(rf.pixel_aspect - CRT) < 1e-3
    assert abs(c.rend.pixel_aspect - CRT) < 1e-3   # live renderer updated
    c.mode = "wire"
    rf = c.frame(0.016, client.InputState())
    assert rf.pixel_aspect == 1.0                  # wire never stretches


def test_pixel_aspect_persists_across_map_change():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.con.execute("pixel_aspect 0.8333333")
    c._cmd_map(["e1m1"])                           # rebuilds the Renderer
    assert abs(c.rend.pixel_aspect - CRT) < 1e-3


def test_pixel_aspect_clamped():
    c = client.Client("e1m1")
    c.con.execute("pixel_aspect 0.1")
    assert c.rend.pixel_aspect == 0.5
    c.con.execute("pixel_aspect 3")
    assert c.rend.pixel_aspect == 1.0


def test_menu_aspect_item_drives_client():
    c = client.Client("e1m1")
    # find the Aspect ChoiceItem and drive it to CRT by cycling to the CRT index
    item = next(i for i in c.menu.items if getattr(i, "title", "") == "Aspect")
    crt_idx = next(i for i, (_, v) in enumerate(item.options) if abs(v - CRT) < 1e-6)
    item.index = crt_idx
    item.on_select(item.options[crt_idx][1])
    assert abs(c.rend.pixel_aspect - CRT) < 1e-3


if __name__ == "__main__":
    test_crt_aspect_widens_vertical_fov()
    test_wire_mode_ignores_pixel_aspect()
    test_renderframe_reports_pixel_aspect()
    test_pixel_aspect_persists_across_map_change()
    test_pixel_aspect_clamped()
    test_menu_aspect_item_drives_client()
    print("OK")
