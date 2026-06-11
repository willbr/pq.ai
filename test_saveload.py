"""Save / load games (Host_Savegame_f / Host_Loadgame_f, host_cmd.c).

The .sav layout is the original's: version, comment, 16 spawn parms, skill,
map name, time, 64 lightstyles, then ED_WriteGlobals' block and one block per
edict (free slots are empty {} blocks). Loading respawns the server normally
(rebuilding precaches, like SV_SpawnServer before the parse) and then
overwrites every edict's fields from the file, so doors stay open, monsters
stay hurt and the player keeps the loadout they saved with.
"""

import os
import tempfile

os.environ.setdefault("PQ_AUDIO", "0")      # headless: no CoreAudio thread

import client
from quake.sv import IT_NAILGUN

PAK = "quake-shareware/id1/pak0.pak"


def _first_monster(sv):
    for e in range(1, sv.vm.num_edicts):
        if sv.vm.free[e]:
            continue
        if sv.pr.string(sv.vm.fget_i(e, sv.f["classname"])) == "monster_army":
            return e
    raise AssertionError("no monster_army on e1m1")


def test_save_then_load_restores_world_state():
    c = client.Client("e1m1")
    c.resize(320, 240)
    sv, f, vm = c.sv, c.sv.f, c.sv.vm
    for _ in range(5):
        sv.run_frame(0.1)

    # distinctive state: a wounded monster, a custom loadout, a freed edict
    mon = _first_monster(sv)
    vm.fset_f(mon, f["health"], 17.0)
    items = int(vm.fget_f(sv.player, f["items"]))
    vm.fset_f(sv.player, f["items"], float(items | IT_NAILGUN))
    vm.fset_f(sv.player, f["ammo_nails"], 66.0)
    doomed = None
    for e in range(mon + 1, vm.num_edicts):
        if not vm.free[e]:
            doomed = e
            vm.free_edict(e)
            break
    saved_time = sv.time
    saved_pos = vm.fget_v(sv.player, f["origin"])

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "quick.sav")
        c.save_game(path)

        # wreck the state, then keep playing a while
        vm.fset_f(mon, f["health"], 80.0)
        vm.fset_f(sv.player, f["ammo_nails"], 0.0)
        for _ in range(10):
            c.sv.run_frame(0.1)

        assert c.load_game(path)

    sv, f, vm = c.sv, c.sv.f, c.sv.vm           # server was rebuilt
    assert sv.mapname == "maps/e1m1.bsp"
    assert abs(sv.time - saved_time) < 1e-3
    assert vm.fget_f(mon, f["health"]) == 17.0, "monster wound not restored"
    assert int(vm.fget_f(sv.player, f["items"])) & IT_NAILGUN
    assert vm.fget_f(sv.player, f["ammo_nails"]) == 66.0
    assert vm.free[doomed], "freed edict came back to life"
    got = vm.fget_v(sv.player, f["origin"])
    assert all(abs(a - b) < 0.5 for a, b in zip(got, saved_pos))
    # the client camera follows the restored player
    assert all(abs(a - b) < 0.5 for a, b in zip(c.pos, saved_pos))
    # and the world still runs
    sv.run_frame(0.1)


def test_load_missing_file_is_refused():
    c = client.Client("e1m1")
    assert c.load_game("/nonexistent/nope.sav") is False


if __name__ == "__main__":
    test_save_then_load_restores_world_state()
    test_load_missing_file_is_refused()
    print("OK")
