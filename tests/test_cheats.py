"""Cheat commands (host_cmd.c): notarget, fly, kill, impulse passthrough.

notarget toggles FL_NOTARGET (QC's FindTarget skips you); fly toggles
MOVETYPE_FLY with collision-clipped free flight; kill runs QC ClientKill;
`impulse 9` reaches W_ImpulseCommands' CheatCommand (all weapons + ammo);
and noclip now stamps the edict's movetype so QC can tell.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from quake.sv import (FL_NOTARGET, MOVETYPE_FLY, MOVETYPE_NOCLIP, MOVETYPE_WALK,
                      IT_ROCKET_LAUNCHER, IT_LIGHTNING)


def _boot():
    c = client.Client("e1m1")
    c.resize(320, 240)
    return c


def test_notarget_toggles_flag():
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    c.con.execute("notarget")
    assert int(vm.fget_f(e, f["flags"])) & FL_NOTARGET
    c.con.execute("notarget")
    assert not (int(vm.fget_f(e, f["flags"])) & FL_NOTARGET)


def test_kill_command_restarts_the_level():
    # single-player QC: ClientKill -> respawn() -> localcmd("restart"), so a
    # console suicide reloads the level rather than leaving a corpse
    c = _boot()
    c.frame(0.05, client.InputState())
    old_sv = c.sv
    c.con.execute("kill")
    c.frame(0.05, client.InputState())
    assert c.sv is not old_sv, "kill did not restart the level"
    assert c.sv.player_health() > 0


def test_impulse_9_gives_all_weapons():
    c = _boot()
    inp = client.InputState()
    c.frame(0.05, inp)
    c.con.execute("impulse 9")
    for _ in range(3):
        c.frame(0.05, inp)
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    items = int(vm.fget_f(e, f["items"]))
    assert items & IT_ROCKET_LAUNCHER and items & IT_LIGHTNING, \
        "impulse 9 did not give all weapons"


def test_fly_mode_flies_but_collides():
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    inp = client.InputState()
    c.con.execute("fly")
    assert int(vm.fget_f(e, f["movetype"])) == MOVETYPE_FLY
    z0 = c.pos[2]
    inp.move_up = 1.0
    for _ in range(10):
        c.frame(0.05, inp)
    assert c.pos[2] > z0 + 20.0, "fly did not ascend"
    # descending into the floor must stop at it, not pass through
    inp.move_up = -1.0
    for _ in range(120):
        c.frame(0.05, inp)
    assert c.pos[2] > z0 - 4096.0, "fell out of the world"
    floor_z = c.pos[2]
    for _ in range(20):
        c.frame(0.05, inp)
    assert abs(c.pos[2] - floor_z) < 1.0, "fly passed through the floor"
    c.con.execute("fly")
    assert int(vm.fget_f(e, f["movetype"])) == MOVETYPE_WALK


def test_noclip_stamps_movetype():
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    c._toggle_noclip()
    assert int(vm.fget_f(e, f["movetype"])) == MOVETYPE_NOCLIP
    c._toggle_noclip()
    assert int(vm.fget_f(e, f["movetype"])) == MOVETYPE_WALK


if __name__ == "__main__":
    test_notarget_toggles_flag()
    test_kill_command_restarts_the_level()
    test_impulse_9_gives_all_weapons()
    test_fly_mode_flies_but_collides()
    test_noclip_stamps_movetype()
    print("OK")
