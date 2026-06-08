"""Regression test: reaching the exit runs intermission and changes level.

Walking into a trigger_changelevel must NOT immediately load the next map.
Quake routes it through intermission (client.qc / triggers.qc):

  1. changelevel_touch sets `nextmap` and schedules execute_changelevel.
  2. execute_changelevel sets intermission_running, freezes every player at an
     info_intermission camera spot (solid=SOLID_NOT, movetype=MOVETYPE_NONE,
     modelindex=0) and sets intermission_exittime.
  3. IntermissionThink (normally from PlayerPreThink) waits for the exit time,
     then on a button press calls GotoNextMap -> changelevel(nextmap).

pq drove none of this: it never read intermission_running, never ran
IntermissionThink, and kept camera-driving the player (who fell while still
holding a visible gun) -- and the level never advanced.

This exercises the server seams the host relies on:
  - Server.intermission_active() reports the QC global.
  - Reaching the exit freezes the player at a spot and sets intermission.
  - Server.run_intermission(button) advances to nextmap after the exit time.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server, SOLID_NOT, MOVETYPE_NONE

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    sv = Server(Progs(pak.read("progs.dat")), bsp=Bsp(pak.read("maps/e1m1.bsp")),
                mapname="maps/e1m1.bsp", skill=1)
    sv.load_level()
    return sv


def _find_exit(sv):
    vm, f, p = sv.vm, sv.f, sv.pr
    for e in range(1, vm.num_edicts):
        if vm.free[e]:
            continue
        if p.string(vm.fget_i(e, f["classname"])) == "trigger_changelevel":
            return e
    raise AssertionError("no trigger_changelevel on e1m1")


def test_reaching_exit_enters_intermission_then_changes_level():
    sv = _boot()
    vm, f = sv.vm, sv.f
    sv.spawn_player((100.0, 100.0, 100.0), (0.0, 0.0, 0.0))
    pl = sv.player

    assert not sv.intermission_active()           # normal play

    # walk into the exit: fire its touch with the player as `other`
    exit_e = _find_exit(sv)
    tf = vm.fget_i(exit_e, f["touch"])
    sv.gset_i("self", exit_e)
    sv.gset_i("other", pl)
    sv.gset_f("time", sv.time)
    vm.execute(tf)

    # execute_changelevel is a scheduled think; a couple of frames lets it fire
    for _ in range(3):
        sv.run_frame(0.1)

    assert sv.intermission_active(), "should be in intermission after the exit"
    # player frozen at the camera spot
    assert vm.fget_f(pl, f["solid"]) == SOLID_NOT
    assert vm.fget_f(pl, f["movetype"]) == MOVETYPE_NONE
    assert int(vm.fget_f(pl, f["modelindex"])) == 0
    # and relocated to an info_intermission origin (not the spawn point)
    org = vm.fget_v(pl, f["origin"])
    assert org != (100.0, 100.0, 100.0), "player should be at the intermission spot"

    # before the exit time, a button does nothing
    sv.run_intermission(button0=True)
    assert sv.changelevel is None

    # after the exit time, a button advances to the trigger's target map
    exittime = sv.gget_f("intermission_exittime")
    sv.time = exittime + 0.5
    sv.run_intermission(button0=True)
    assert sv.changelevel == "e1m2", f"expected e1m2, got {sv.changelevel!r}"


if __name__ == "__main__":
    test_reaching_exit_enters_intermission_then_changes_level()
    print("OK")
