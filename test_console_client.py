"""Boots the real engine stack (needs quake-shareware/id1/pak0.pak) and tests
the Renderer's live zbuf_scale and the Client's console bindings."""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer, ZBUF_SCALE
from quake.sv import FL_GODMODE


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


def _boot_server():
    from quake.progs import Progs
    from quake.sv import Server
    from quake.physics import Physics
    pak = Pak("quake-shareware/id1/pak0.pak")
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=bsp,
                mapname="maps/e1m1.bsp", skill=1, pak=pak)
    sv.phys = Physics(bsp)
    sv.load_level()
    (sx, sy, sz), yaw = bsp.find_spawn()
    sv.spawn_player((sx, sy, sz), (0.0, yaw, 0.0))
    return sv


def test_toggle_god_flips_flag():
    sv = _boot_server()
    f = sv.f["flags"]
    base = int(sv.vm.fget_f(sv.player, f))
    assert sv.toggle_god() is True
    assert int(sv.vm.fget_f(sv.player, f)) & FL_GODMODE
    assert sv.toggle_god() is False
    assert int(sv.vm.fget_f(sv.player, f)) == base


def test_give_health_and_ammo():
    sv = _boot_server()
    sv.give("h", 25)
    assert int(sv.vm.fget_f(sv.player, sv.f["health"])) == 25
    sv.give("r", 99)
    assert int(sv.vm.fget_f(sv.player, sv.f["ammo_rockets"])) == 99
    assert "unknown" in sv.give("zzz", None).lower()


if __name__ == "__main__":
    test_renderer_zbuf_scale_is_live()
    test_toggle_god_flips_flag()
    test_give_health_and_ammo()
    print("OK")
