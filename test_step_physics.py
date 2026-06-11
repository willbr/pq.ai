"""MOVETYPE_STEP physics (SV_Physics_Step, WinQuake sv_phys.c).

Walking monsters are MOVETYPE_STEP: their locomotion comes from the
walkmove/movetogoal builtins, but gravity/velocity integration comes from
SV_Physics_Step when FL_ONGROUND|FL_FLY|FL_SWIM is clear. That's what makes
knockback work -- QC's T_Damage strips FL_ONGROUND and adds velocity, then
step physics flies the monster until it lands (with the demon/dland2.wav thud
on a hard landing). SV_CheckWaterTransition stamps watertype/waterlevel and
splashes on crossing a water boundary.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.physics import Physics, CONTENTS_EMPTY, CONTENTS_WATER
from quake.progs import Progs
from quake.sv import Server, MOVETYPE_STEP, FL_ONGROUND

PAK = "quake-shareware/id1/pak0.pak"


class SoundLog:
    """Stand-in mixer: records start_sound calls."""
    def __init__(self):
        self.played = []

    def start_sound(self, ent, chan, sample, vol, atten, origin, loop=False):
        self.played.append(sample)


def _boot():
    pak = Pak(PAK)
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=bsp, mapname="maps/e1m1.bsp",
                skill=1, physics=Physics(bsp), pak=pak)
    sv.load_level()
    sv.snd = SoundLog()
    return sv


def _find_monster(sv):
    """First spawned monster_army edict, with its AI thinks suppressed."""
    for e in range(1, sv.vm.num_edicts):
        if sv.vm.free[e]:
            continue
        cn = sv.pr.string(sv.vm.fget_i(e, sv.f["classname"]))
        if cn == "monster_army":
            sv.vm.fset_f(e, sv.f["nextthink"], 1.0e9)   # no AI during the test
            assert int(sv.vm.fget_f(e, sv.f["movetype"])) == MOVETYPE_STEP
            return e
    raise AssertionError("no monster_army on e1m1 at skill 1")


def test_airborne_monster_falls_and_lands():
    sv = _boot()
    e = _find_monster(sv)
    f, vm = sv.f, sv.vm
    ox, oy, oz = vm.fget_v(e, f["origin"])
    vm.fset_v(e, f["origin"], (ox, oy, oz + 60.0))      # hoist into the air
    # the hoisted hull must be in open space, or the freefall is (correctly)
    # suppressed as trapped-in-solid, like SV_FlyMove's allsolid case
    tr = sv._box_move(e, (ox, oy, oz + 60.0), (ox, oy, oz + 59.0))
    assert not tr.allsolid, "test spot is inside geometry; pick another hoist"
    flags = int(vm.fget_f(e, f["flags"]))
    vm.fset_f(e, f["flags"], float(flags & ~FL_ONGROUND))

    for _ in range(40):                                 # 2s of 50ms frames
        sv.run_frame(0.05)

    nx, ny, nz = vm.fget_v(e, f["origin"])
    assert nz < oz + 10.0, f"monster did not fall: z {oz + 60:.0f} -> {nz:.0f}"
    assert int(vm.fget_f(e, f["flags"])) & FL_ONGROUND, "did not land"
    assert vm.fget_v(e, f["velocity"])[2] == 0.0
    # 60 units of freefall lands at ~310 u/s, past the 80 u/s thud threshold
    assert "demon/dland2.wav" in sv.snd.played, "no landing thud"


def test_knockback_velocity_integrates():
    sv = _boot()
    e = _find_monster(sv)
    f, vm = sv.f, sv.vm
    ox, oy, oz = vm.fget_v(e, f["origin"])
    # as QC T_Damage does: strip FL_ONGROUND and shove
    flags = int(vm.fget_f(e, f["flags"]))
    vm.fset_f(e, f["flags"], float(flags & ~FL_ONGROUND))
    vm.fset_v(e, f["velocity"], (-120.0, 0.0, 120.0))   # away from the wall

    for _ in range(20):
        sv.run_frame(0.05)

    nx, ny, nz = vm.fget_v(e, f["origin"])
    moved = abs(nx - ox) + abs(ny - oy)
    assert moved > 20.0, f"knockback did not move the monster ({moved:.1f} units)"
    assert int(vm.fget_f(e, f["flags"])) & FL_ONGROUND, "did not land again"


def test_water_transition_splashes():
    sv = _boot()
    e = _find_monster(sv)
    f, vm = sv.f, sv.vm
    # the monster stands in open air but believes it's underwater: crossing
    # the boundary must splash and restamp watertype/waterlevel
    vm.fset_f(e, f["watertype"], float(CONTENTS_WATER))
    sv.check_water_transition(e)
    assert "misc/h2ohit1.wav" in sv.snd.played, "no splash on leaving water"
    assert int(vm.fget_f(e, f["watertype"])) == CONTENTS_EMPTY


if __name__ == "__main__":
    test_airborne_monster_falls_and_lands()
    test_knockback_velocity_integrates()
    test_water_transition_splashes()
    print("OK")
