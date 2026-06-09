"""Tests for the video-options resolution path: the Renderer's video_res
override (fixed z-buffer framebuffer size) and the Client's video state +
menu wiring. Boots the real shareware stack, like the other client tests."""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer, ZBUF_SCALE
from client import Client


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


def test_client_default_video_res_is_320x240():
    c = Client("e1m1")
    assert c.video_res == (320, 240)
    assert c.rend.video_res == (320, 240)
    assert c.rend.zw == 320 and c.rend.zh == 240


def test_set_video_res_rebuilds_buffer_immediately():
    c = Client("e1m1")
    c.resize(800, 600)
    c.set_video_res((640, 480))
    assert c.video_res == (640, 480)
    assert c.rend.zw == 640 and c.rend.zh == 480


def test_video_res_persists_across_map_change():
    c = Client("e1m1")
    c.set_video_res((240, 160))
    c._cmd_map(["e1m1"])             # rebuilds rend
    c.resize(800, 600)               # frontend resizes after a map load
    assert c.rend.video_res == (240, 160)
    assert c.rend.zw == 240 and c.rend.zh == 160


def test_menu_resolution_item_drives_client():
    c = Client("e1m1")
    c.resize(800, 600)
    # the first menu item is the Resolution choice, wired to set_video_res
    c.menu.selected = 0
    c.menu.key_right()               # cycle off the default (320x240) to 640x480
    assert c.video_res == (640, 480)
    assert c.rend.zw == 640 and c.rend.zh == 480


if __name__ == "__main__":
    test_renderer_video_res_fixes_framebuffer()
    test_client_default_video_res_is_320x240()
    test_set_video_res_rebuilds_buffer_immediately()
    test_video_res_persists_across_map_change()
    test_menu_resolution_item_drives_client()
    print("OK")
