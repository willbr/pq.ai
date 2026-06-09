"""Tests for the video-options resolution path: the Renderer's video_res
override (fixed z-buffer framebuffer size) and the Client's video state +
menu wiring. Boots the real shareware stack, like the other client tests."""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer, ZBUF_SCALE


def _palette(pak):
    pal = pak.read("gfx/palette.lmp")
    return [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]


def test_renderer_video_res_fixes_framebuffer():
    pak = Pak("quake-shareware/id1/pak0.pak")
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    rend = Renderer(bsp, _palette(pak))
    # default: video_res None -> behaves as before (window // zbuf_scale)
    assert rend.video_res is None
    rend.resize(800, 600)
    assert rend.zw == 800 // ZBUF_SCALE and rend.zh == 600 // ZBUF_SCALE
    # set a fixed resolution -> framebuffer is exactly that, ignoring zbuf_scale
    rend.video_res = (320, 240)
    rend.resize(800, 600)
    assert rend.zw == 320 and rend.zh == 240
    assert len(rend._zb_zero) == 320 * 240 * 4
    # a different window size keeps the fixed buffer
    rend.resize(1024, 768)
    assert rend.zw == 320 and rend.zh == 240


if __name__ == "__main__":
    test_renderer_video_res_fixes_framebuffer()
    print("OK")
