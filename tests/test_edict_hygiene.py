"""Edict lifetime + velocity hygiene (ED_Alloc/ED_Free, SV_CheckVelocity).

ED_Alloc refuses to reuse a slot freed less than 0.5s ago (except in the
level's first 2 seconds), so a grenade's slot isn't recycled into a monster
mid-explosion. ED_Free clears only the visible/collidable fields (model,
modelindex, solid, origin, ...) rather than wiping the edict, matching
pr_edict.c. SV_CheckVelocity clamps runaway/NaN velocity to +/-2000 before
physics runs an entity.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server, MOVETYPE_BOUNCE

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    sv = Server(Progs(pak.read("progs.dat")),
                bsp=Bsp(pak.read("maps/e1m1.bsp")),
                mapname="maps/e1m1.bsp", skill=1)
    sv.load_level()
    return sv


def test_fresh_slot_not_reused_for_half_a_second():
    sv = _boot()
    vm = sv.vm
    # past the 2-second level-start grace
    for _ in range(25):
        sv.run_frame(0.1)
    e = vm.alloc_edict()
    vm.free_edict(e)
    e2 = vm.alloc_edict()
    assert e2 != e, "slot reused immediately after free"
    for _ in range(6):                       # 0.6 s later it's fair game
        sv.run_frame(0.1)
    e3 = vm.alloc_edict()
    assert e3 == e, "slot never became reusable"


def test_level_start_reuses_freely():
    sv = _boot()
    vm = sv.vm
    e = vm.alloc_edict()                     # sv.time is still ~0
    vm.free_edict(e)
    assert vm.alloc_edict() == e, "early reuse should be allowed (< 2s)"


def test_free_edict_clears_visible_fields_only():
    sv = _boot()
    vm, f = sv.vm, sv.f
    e = vm.alloc_edict()
    vm.fset_i(e, f["classname"], sv.pr.new_string("grenade"))
    vm.fset_i(e, f["modelindex"], 3)
    vm.fset_f(e, f["solid"], 2.0)
    vm.fset_v(e, f["origin"], (10.0, 20.0, 30.0))
    vm.fset_f(e, f["health"], 77.0)
    vm.free_edict(e)
    assert vm.free[e]
    assert vm.fget_i(e, f["modelindex"]) == 0
    assert vm.fget_f(e, f["solid"]) == 0.0
    assert vm.fget_v(e, f["origin"]) == (0.0, 0.0, 0.0)
    assert vm.fget_f(e, f["nextthink"]) == -1.0
    # ED_Free leaves the rest alone (C only zeroes the visible fields)
    assert vm.fget_f(e, f["health"]) == 77.0


def test_check_velocity_clamps_runaway():
    sv = _boot()
    vm, f = sv.vm, sv.f
    e = vm.alloc_edict()
    vm.fset_f(e, f["movetype"], float(MOVETYPE_BOUNCE))
    vm.fset_f(e, f["solid"], 0.0)
    vm.fset_v(e, f["velocity"], (99999.0, -99999.0, float("nan")))
    sv.run_frame(0.05)
    vx, vy, vz = vm.fget_v(e, f["velocity"])
    assert vx <= 2000.0 and vy >= -2000.0, f"velocity not clamped: {vx},{vy}"
    assert vz == vz, "NaN velocity survived"      # NaN != NaN


if __name__ == "__main__":
    test_fresh_slot_not_reused_for_half_a_second()
    test_level_start_reuses_freely()
    test_free_edict_clears_visible_fields_only()
    test_check_velocity_clamps_runaway()
    print("OK")
