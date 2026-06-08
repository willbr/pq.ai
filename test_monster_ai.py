"""Regression test: monsters acquire the player and chase them.

The monster AI was inert. Two engine builtins the QC relies on were stubs:

  - checkclient (#17) returned 0, so FindTarget never saw the player and no
    monster ever set .enemy -- they stood idle forever.
  - movetogoal / walkmove / checkbottom / droptofloor (#67/#32/#40/#34) did
    nothing, so even an alerted monster could not step toward its goal, and
    monsters never settled onto the floor at spawn.

With these implemented (SV_CheckClient + SV_MoveToGoal/SV_movestep and friends),
a monster that can see the player in front of it acquires them as .enemy and
walks measurably closer.

Driven against the real shareware progs on e1m1. Monsters are placed above the
floor in the map and drop onto it via droptofloor on their first think, so the
test settles them for a few frames before positioning the player, and uses the
QC's own visible()/infront() as the line-of-sight oracle.
"""

import math
from pak import Pak
from bsp import Bsp
from progs import Progs
from sv import Server
from physics import Physics

PAK = "quake-shareware/id1/pak0.pak"
OFS_PARM0 = 4
OFS_RETURN = 1


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname="maps/e1m1.bsp", skill=1)
    sv.phys = Physics(b)
    sv.load_level()
    return sv


def _qcall(sv, fn, me, arg):
    """Invoke a QC function (e.g. visible/infront) with self=me, arg in PARM0."""
    g = sv.pr.find_function(fn)
    sv.gset_i("self", me)
    sv.gset_i("other", 0)
    sv.gset_f("time", sv.time)
    sv.vm.gi[OFS_PARM0] = arg
    sv.vm.execute(g)
    return sv.vm.gf[OFS_RETURN]


def test_monster_acquires_and_chases_player():
    sv = _boot()
    vm, f, p = sv.vm, sv.f, sv.pr

    # park the player far away and let every monster droptofloor / settle
    sv.spawn_player((10000.0, 10000.0, 10000.0), (0.0, 0.0, 0.0))
    for _ in range(15):
        sv.run_frame(0.1)

    monsters = [e for e in range(1, vm.num_edicts)
                if not vm.free[e]
                and p.string(vm.fget_i(e, f["classname"])).startswith("monster_")]
    assert monsters, "no monsters on e1m1"

    # find a settled monster with a clear, in-front spot 120 units away
    chosen = None
    for me in monsters:
        o = vm.fget_v(me, f["origin"])
        for deg in range(0, 360, 30):
            r = math.radians(deg)
            spot = (o[0] + math.cos(r) * 120.0, o[1] + math.sin(r) * 120.0, o[2])
            vm.fset_v(sv.player, f["origin"], spot)
            sv._link_abs(sv.player)
            vm.fset_v(me, f["angles"], (0.0, float(deg), 0.0))
            vm.fset_f(me, f["ideal_yaw"], float(deg))
            if _qcall(sv, "visible", me, sv.player) == 1.0 and \
                    _qcall(sv, "infront", me, sv.player) == 1.0:
                chosen = me
                break
        if chosen is not None:
            break
    assert chosen is not None, "could not place the player in a monster's sight"

    def dist_xy():
        mo = vm.fget_v(chosen, f["origin"])
        po = vm.fget_v(sv.player, f["origin"])
        return math.hypot(mo[0] - po[0], mo[1] - po[1])

    start = dist_xy()
    acquired = False
    for _ in range(60):                       # up to 6 seconds
        sv.run_frame(0.1)
        if vm.fget_i(chosen, f["enemy"]) == sv.player:
            acquired = True

    assert acquired, "monster never set .enemy to the player"
    assert dist_xy() < start - 40.0, (
        f"monster did not chase: {start:.0f} -> {dist_xy():.0f} units")


if __name__ == "__main__":
    test_monster_acquires_and_chases_player()
    print("OK")
