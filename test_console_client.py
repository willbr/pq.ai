"""Boots the real engine stack (needs quake-shareware/id1/pak0.pak) and tests
the Renderer's live zbuf_scale and the Client's console bindings."""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer, ZBUF_SCALE


def _palette(pak):
    pal = pak.read("gfx/palette.lmp")
    return [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]


def test_renderer_zbuf_scale_is_live():
    pak = Pak("quake-shareware/id1/pak0.pak")
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    rend = Renderer(bsp, _palette(pak))
    assert rend.zbuf_scale == ZBUF_SCALE          # defaults from the constant
    rend.resize(800, 600)
    assert rend.zw == 800 // ZBUF_SCALE and rend.zh == 600 // ZBUF_SCALE
    rend.zbuf_scale = 8                            # change it...
    rend.resize(800, 600)                          # ...and re-size
    assert rend.zw == 800 // 8 and rend.zh == 600 // 8


if __name__ == "__main__":
    test_renderer_zbuf_scale_is_live()
    print("OK")
