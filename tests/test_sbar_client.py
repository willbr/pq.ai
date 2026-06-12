"""Full-stack tests for the sprite status bar: raw hud_status fields, the
viewport shrink, the framebuffer composite and the text-HUD fallback.
Boots the shareware stack like the other client tests."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import client
from quake.sbar import IT_SHOTGUN


def test_hud_status_raw_fields():
    c = client.Client("e1m1")
    st = c.sv.hud_status()
    assert st["items"] & IT_SHOTGUN          # spawn gives the shotgun
    assert st["weapon_bit"] == IT_SHOTGUN    # ...and selects it
    assert isinstance(st["items"], int)
    # e1m1 sets no serverflags yet: sigil bits 28..31 clear at spawn
    assert st["items"] >> 28 == 0
    # existing text-HUD keys are untouched
    assert st["weapon"] == "Shotgun" and "health" in st


def test_renderer_sbar_lines_shrinks_view():
    from quake.pak import Pak
    from quake.bsp import Bsp
    from quake.render import Renderer
    pak = Pak("quake-shareware/id1/pak0.pak")
    pal = pak.read("gfx/palette.lmp")
    palette = [(pal[i*3], pal[i*3+1], pal[i*3+2]) for i in range(256)]
    rend = Renderer(Bsp(pak.read("maps/e1m1.bsp")), palette)
    rend.video_res = (320, 200)
    rend.sbar_lines = 48
    rend.resize(800, 600)
    assert rend.zw == 320 and rend.zh == 152      # view above the bar
    assert len(rend._zb_far) == 320 * 152
    rend.sbar_lines = 0
    rend.resize(800, 600)
    assert rend.zh == 200                          # full height again
    # auto mode shrinks the window-derived size the same way
    rend.video_res = None
    rend.zbuf_scale = 2
    rend.sbar_lines = 48
    rend.resize(800, 600)
    assert rend.zw == 400 and rend.zh == 300 - 48


def _boot_zbuf(res):
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "zbuf"
    c.set_video_res(res)
    return c


def _overlay_text(rf):
    return " ".join(o[2] for o in rf.overlays)


def test_default_video_res_is_320x200():
    assert client.DEFAULT_VIDEO_RES == (320, 200)
    assert ("320x200", (320, 200)) in client.VIDEO_MODES


def test_sprite_bar_composited_at_320x200():
    c = _boot_zbuf((320, 200))
    rf = c.frame(0.016, client.InputState())
    fb, w, h = rf.framebuffer
    assert (w, h) == (320, 200)                  # full screen incl. bar rows
    assert c.rend.zh == 152                      # 3D view shrunk above it
    # the sbar strip landed: compare an untouched column (x=210)
    from quake.pak import Pak
    from quake.wad import Wad
    wad = Wad(Pak("quake-shareware/id1/pak0.pak").read("gfx.wad"))
    sw, sh, spx = wad.qpic("sbar")
    assert all(fb[(200 - 24 + r) * 320 + 210] == spx[r * sw + 210]
               for r in range(sh))
    # text status bar suppressed (diagnostics HUD line stays)
    assert "HEALTH" not in _overlay_text(rf)


def test_narrow_res_falls_back_to_text():
    c = _boot_zbuf((240, 160))
    rf = c.frame(0.016, client.InputState())
    fb, w, h = rf.framebuffer
    assert (w, h) == (240, 160)
    assert c.rend.sbar_lines == 0 and c.rend.zh == 160
    assert "HEALTH" in _overlay_text(rf)


def test_wire_mode_keeps_text_bar():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "wire"
    rf = c.frame(0.016, client.InputState())
    assert "HEALTH" in _overlay_text(rf)


def test_pain_face_timer_set_on_damage():
    c = _boot_zbuf((320, 200))
    c.frame(0.016, client.InputState())
    t0 = c.faceanimtime
    # stamp damage on the player edict the way T_Damage does, then frame
    vm, f, e = c.sv.vm, c.sv.f, c.sv.player
    vm.fset_f(e, f["dmg_take"], 10.0)
    c.frame(0.016, client.InputState())
    assert c.faceanimtime > t0
    assert c.faceanimtime <= c.sv.time + 0.2 + 1e-6


def test_hud_timers_reset_on_level_change():
    # CL_ClearState: the cosmetic HUD timers must not leak the old level's
    # clock into the new one (sv.time restarts at 0 per level)
    c = _boot_zbuf((320, 200))
    c.frame(0.016, client.InputState())
    c.faceanimtime = 50.0
    c.item_gettime[3] = 120.0
    c._cmd_map(["e1m1"])                 # console "map" -> _load_map
    assert c.faceanimtime == 0.0
    assert c.item_gettime == [0.0] * 32
    assert c._prev_items == 0
    c.frame(0.016, client.InputState())  # and the new level still renders


if __name__ == "__main__":
    test_hud_status_raw_fields()
    test_renderer_sbar_lines_shrinks_view()
    test_default_video_res_is_320x200()
    test_sprite_bar_composited_at_320x200()
    test_narrow_res_falls_back_to_text()
    test_wire_mode_keeps_text_bar()
    test_pain_face_timer_set_on_damage()
    test_hud_timers_reset_on_level_change()
    print("OK")
