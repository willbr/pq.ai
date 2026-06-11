"""Regression test: the player actually dies, and can respawn.

Death did nothing. The engine hand-builds the client edict (it drives the live
player from the camera) instead of running PutClientInServer, and three pieces
of the death path were therefore missing:

  - .th_die was never set, so combat.qc's Killed() called a null function --
    PlayerDie never ran, the player just kept walking at 0 health.
  - PlayerDeathThink (the dead->respawnable->respawn FSM, normally reached via
    PlayerPreThink) was never driven server-side.
  - localcmd (#46) was a no-op, so single-player respawn()'s "restart" -- which
    reloads the level -- went nowhere.

With th_die wired in spawn_player, run_player_death_think driven each frame, and
localcmd routing "restart" to the host's changelevel path, the player dies into
a MOVETYPE_TOSS corpse with the view dropped to the floor, and a fire press
after the body settles requests a level restart.

Driven against the real shareware progs on e1m1.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server, MOVETYPE_TOSS, DEAD_DEAD
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname="maps/e1m1.bsp", skill=1)
    sv.phys = Physics(b)
    sv.load_level()
    sv.spawn_player((480.0, -352.0, 88.0), (0.0, 0.0, 0.0))
    return sv


def test_death_runs_and_respawns():
    sv = _boot()
    e, vm, f = sv.player, sv.vm, sv.f

    # th_die must point at PlayerDie, or Killed() no-ops and death is invisible.
    assert vm.fget_i(e, f["th_die"]) != 0, "th_die not wired to PlayerDie"

    # Die the way combat.qc's Killed() does: lethal health, then self.th_die().
    vm.fset_f(e, f["health"], -10.0)
    sv.gset_f("time", sv.time)
    sv.gset_i("self", e)
    sv.gset_i("other", e)
    vm.execute(vm.fget_i(e, f["th_die"]))

    # PlayerDie's fingerprints: a tossed corpse, holstered weapon, sunken view.
    assert vm.fget_f(e, f["deadflag"]) >= 1.0, "player never entered death"
    assert int(vm.fget_f(e, f["movetype"])) == MOVETYPE_TOSS, "corpse not tossed"
    assert sv.pr.string(vm.fget_i(e, f["weaponmodel"])) == "", "weapon not dropped"
    assert vm.fget_v(e, f["view_ofs"])[2] < 0.0, "view did not drop to the body"

    # Let the death animation settle to DEAD_DEAD/RESPAWNABLE with fire released.
    sv.button0 = False
    for _ in range(60):
        sv.run_frame(0.1)
    assert vm.fget_f(e, f["deadflag"]) >= DEAD_DEAD, "death never settled"

    # Fire pressed -> PlayerDeathThink -> respawn() -> localcmd("restart") ->
    # host changelevel back into the same level.
    sv.button0 = True
    for _ in range(10):
        sv.run_frame(0.1)
        if sv.changelevel:
            break
    assert sv.changelevel == "e1m1", "respawn did not request a level restart"


def test_corpse_rests_on_the_floor_not_through_it():
    """The dead player is a MOVETYPE_TOSS corpse that keeps the player bbox, so it
    must sweep its BOX and rest its box bottom on the floor (SV_PushEntity's
    SV_Move). Tracing it as a point sank its origin to the floor, putting the
    death-cam eye (origin + view_ofs.z = origin - 8) below the floor -- the
    'death cam noclips through the floor a little'."""
    sv = _boot()
    e, vm, f = sv.player, sv.vm, sv.f
    floor = vm.fget_v(e, f["origin"])[2] + vm.fget_v(e, f["mins"])[2]   # living feet
    vm.fset_f(e, f["health"], -20.0)
    sv.gset_f("time", sv.time); sv.gset_i("self", e); sv.gset_i("other", e)
    vm.execute(vm.fget_i(e, f["th_die"]))
    for _ in range(40):
        sv.run_frame(0.05)                            # let the corpse settle
    org = vm.fget_v(e, f["origin"])
    box_bottom = org[2] + vm.fget_v(e, f["mins"])[2]
    eye_z = org[2] + vm.fget_v(e, f["view_ofs"])[2]
    assert box_bottom > floor - 4.0, \
        f"corpse sank through the floor (box bottom {box_bottom:.0f} vs floor {floor:.0f})"
    assert eye_z > box_bottom, \
        f"death-cam eye ({eye_z:.0f}) is below the corpse's footing ({box_bottom:.0f})"


if __name__ == "__main__":
    test_death_runs_and_respawns()
    test_corpse_rests_on_the_floor_not_through_it()
    print("OK")
