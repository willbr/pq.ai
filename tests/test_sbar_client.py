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


if __name__ == "__main__":
    test_hud_status_raw_fields()
    test_renderer_sbar_lines_shrinks_view()
    print("OK")
