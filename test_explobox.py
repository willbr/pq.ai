"""Regression test: the misc_explobox is solid, shootable, and explodes.

The barrel didn't work: setmodel only set an entity's bounding box for inline
'*N' submodels, giving external brush models (maps/b_explob.bsp) a zero-size
box. misc_explobox relies entirely on setmodel for its bounds (unlike items,
which call setsize), so the barrel ended up with absmin == absmax == origin --
a zero-volume box that bullets pass straight through. Quake's setmodel sets a
brush model's size from the model bounds; reading them from the pak fixes it.

Driven against the real shareware progs on e1m1.
"""

from pak import Pak
from bsp import Bsp
from progs import Progs, OFS_PARM0, OFS_PARM_STRIDE
from sv import Server
from physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname="maps/e1m1.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    sv.spawn_player((480.0, -352.0, 88.0), (0.0, 0.0, 0.0))
    for _ in range(10):
        sv.run_frame(0.1)
    return sv


def _find_explobox(sv):
    vm, f = sv.vm, sv.f
    for num in range(1, vm.num_edicts):
        if vm.free[num]:
            continue
        if sv.pr.string(vm.fget_i(num, f["classname"])) == "misc_explobox":
            return num
    raise AssertionError("no misc_explobox on e1m1")


def test_explobox_has_solid_box():
    sv = _boot()
    vm, f = sv.vm, sv.f
    ex = _find_explobox(sv)
    amn = vm.fget_v(ex, f["absmin"])
    amx = vm.fget_v(ex, f["absmax"])
    vol = (amx[0] - amn[0]) * (amx[1] - amn[1]) * (amx[2] - amn[2])
    assert vol > 0.0, f"explobox box is degenerate: {amn}..{amx}"
    # and a shot through its centre must register a hit on it
    cx = (amn[0] + amx[0]) * 0.5
    cy = (amn[1] + amx[1]) * 0.5
    cz = (amn[2] + amx[2]) * 0.5
    _, _, _, _, _, hit = sv._move_trace((cx - 200, cy, cz),
                                        (cx + 200, cy, cz), 0, sv.player)
    assert hit == ex, f"traceline missed the explobox (hit {hit}, want {ex})"


def test_explobox_explodes_when_killed():
    sv = _boot()
    vm, f = sv.vm, sv.f
    ex = _find_explobox(sv)
    # T_Damage(targ=ex, inflictor=player, attacker=player, damage=50)
    fn = sv.pr.find_function("T_Damage")
    for i, val in ((0, ex), (1, sv.player), (2, sv.player)):
        vm.gi[OFS_PARM0 + i * OFS_PARM_STRIDE] = val
    vm.gf[OFS_PARM0 + 3 * OFS_PARM_STRIDE] = 50.0
    sv.gset_i("self", ex)
    sv.gset_i("other", sv.player)
    sv.gset_f("time", sv.time)
    vm.execute(fn)
    assert vm.fget_f(ex, f["health"]) <= 0, "explobox took no lethal damage"
    for _ in range(20):
        sv.run_frame(0.1)
    assert vm.free[ex], "explobox never exploded (still in the world)"


if __name__ == "__main__":
    test_explobox_has_solid_box()
    test_explobox_explodes_when_killed()
    print("OK")
