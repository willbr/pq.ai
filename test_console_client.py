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
    assert len(rend._zb_zero) == rend.zw * rend.zh * 4


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


def test_client_console_bindings():
    from client import Client
    c = Client("e1m1")
    c.resize(800, 600)
    # render toggles run by name and flip the same flags the keys do
    before = c.noclip
    c.con.execute("noclip")
    assert c.noclip != before
    # zbuf_scale cvar resizes the framebuffer (Auto mode) and persists on the client
    c.set_video_res(None)                         # Auto: zbuf_scale drives the framebuffer size
    c.con.execute("zbuf_scale 8")
    assert c.rend.zbuf_scale == 8
    assert c.rend.zw == 800 // 8
    assert c._zbuf_scale == 8
    # clamps out-of-range
    c.con.execute("zbuf_scale 999")
    assert c.rend.zbuf_scale == 16
    # quit command sets the flag the frontend loop watches
    assert c.quit_requested is False
    c.con.execute("quit")
    assert c.quit_requested is True


def test_client_map_command_persists_zbuf_scale():
    from client import Client
    c = Client("e1m1")
    c.resize(640, 480)
    c.con.execute("zbuf_scale 2")
    c.con.execute("map e1m2")                 # changelevel rebuilds the Renderer
    assert c.rend.zbuf_scale == 2             # ...but the chosen scale carries over


def test_console_god_give_and_set():
    from client import Client
    c = Client("e1m1")
    c.resize(640, 480)
    # god toggles the player's FL_GODMODE flag through the bound command
    from quake.sv import FL_GODMODE
    f = c.sv.f["flags"]
    base = int(c.sv.vm.fget_f(c.sv.player, f)) & FL_GODMODE
    c.con.execute("god")
    assert (int(c.sv.vm.fget_f(c.sv.player, f)) & FL_GODMODE) != base
    # give sets an ammo pool
    c.con.execute("give r 42")
    assert int(c.sv.vm.fget_f(c.sv.player, c.sv.f["ammo_rockets"])) == 42
    # set writes a float into the QuakeC cvar dict
    c.con.execute("set teamplay 1")
    assert c.sv.cvars["teamplay"] == 1.0


def test_console_alias_expands_through_client():
    from client import Client
    c = Client("e1m1")
    c.resize(640, 480)
    before = c.noclip
    c.con.execute("alias nc noclip")
    c.con.execute("nc")
    assert c.noclip != before


def test_console_renderframe_payload_when_active():
    from client import Client, InputState
    c = Client("e1m1")
    c.resize(800, 600)
    c.con.active = True
    for ch in "map":            # type into the console line
        c.con.key_char(ch)
    rf = c.frame(0.016, InputState())
    assert rf.console is not None
    lines, input_line, cursor_col = rf.console
    assert isinstance(lines, list)
    assert input_line == "]map"
    assert cursor_col == c.con.cursor + 1
    # closed -> no payload
    c.con.active = False
    rf2 = c.frame(0.016, InputState())
    assert rf2.console is None


if __name__ == "__main__":
    test_renderer_zbuf_scale_is_live()
    test_toggle_god_flips_flag()
    test_give_health_and_ammo()
    test_client_console_bindings()
    test_client_map_command_persists_zbuf_scale()
    test_console_god_give_and_set()
    test_console_alias_expands_through_client()
    test_console_renderframe_payload_when_active()
    print("OK")
