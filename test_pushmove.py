"""Regression tests for SV_PushMove (brush movers carrying/pushing the player).

History of the bug:
  * Doors carried the player whenever their boxes merely overlapped, with no
    block test -- so touching a door dragged the player its full travel, through
    walls and out of the level.
  * Restricting carry to "feet on top" stopped the door drag but, with still no
    block test, a moving lift/train on e1m3 shoved a rider into adjacent geometry
    and stuck them.

The fix ports WinQuake's SV_PushMove: a mover displaces each entity that rides
it (on top) or that it moves into (real hull penetration test), and BLOCK-TESTS
the result -- if the new spot is world-solid the entity is left where it fits and
the mover's .blocked fires. So the player is carried correctly and is never
rammed out of bounds.

Driven against the real shareware progs on e1m1.
"""

import math
from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname="maps/e1m1.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    for _ in range(3):
        sv.run_frame(0.1)
    return sv


def _off(sv, n):
    return sv.pr.field_by_name[n][1]


def test_door_push_never_leaves_player_out_of_bounds():
    """A door opening while the player overlaps it may shove them aside (Quake
    does), but must never leave them embedded in world solid (out of bounds)."""
    sv = _boot()
    vm = sv.vm
    mn = vm.fget_v(7, _off(sv, "mins")); mx = vm.fget_v(7, _off(sv, "maxs"))
    cx = (mn[0] + mx[0]) * 0.5; cy = (mn[1] + mx[1]) * 0.5
    sv.spawn_player((cx, cy, mn[2] + 24.0), (0.0, 0.0, 0.0))
    sv.gset_f("time", sv.time); sv.gset_i("self", 7); sv.gset_i("other", sv.player)
    vm.execute(vm.fget_i(7, _off(sv, "use")))
    for _ in range(10):
        sv.run_frame(0.1)
        org = vm.fget_v(sv.player, _off(sv, "origin"))
        assert not sv.phys.test_position(org, vm.fget_v(sv.player, _off(sv, "mins"))), \
            f"door pushed the player out of bounds to {org}"


def _a_plat(sv):
    vm = sv.vm
    for n in range(1, vm.num_edicts):
        if not vm.free[n] and sv.pr.string(vm.fget_i(n, _off(sv, "classname"))) == "plat":
            return n
    raise AssertionError("no plat on e1m1")


def test_lift_carries_a_rider_standing_on_top():
    sv = _boot()
    vm = sv.vm
    plat = _a_plat(sv)
    amn = vm.fget_v(plat, _off(sv, "absmin")); amx = vm.fget_v(plat, _off(sv, "absmax"))
    cx = (amn[0] + amx[0]) * 0.5; cy = (amn[1] + amx[1]) * 0.5
    # stand on top: player feet (origin - 24) at the plat's top surface
    sv.spawn_player((cx, cy, amx[2] + 24.0), (0.0, 0.0, 0.0))
    z0 = vm.fget_v(sv.player, _off(sv, "origin"))[2]
    sv.player_carry = [0.0, 0.0, 0.0]
    vm.fset_v(plat, _off(sv, "velocity"), (0.0, 0.0, 100.0))   # rise
    sv._push_move(plat, 0.1)
    z1 = vm.fget_v(sv.player, _off(sv, "origin"))[2]
    assert abs((z1 - z0) - 10.0) < 0.5, f"rider not carried up with the lift ({z1 - z0})"
    assert abs(sv.player_carry[2] - 10.0) < 0.5, "carry not reported to the camera"


def test_mover_does_not_drag_a_clear_bystander():
    """A player standing well clear of a mover (not on it, not penetrated by it)
    must not be dragged when it moves -- that was the door-drag bug."""
    sv = _boot()
    vm = sv.vm
    plat = _a_plat(sv)
    amn = vm.fget_v(plat, _off(sv, "absmin")); amx = vm.fget_v(plat, _off(sv, "absmax"))
    # park the player far from the plat, in open space
    (sx, sy, sz), _ = sv.bsp.find_spawn()
    sv.spawn_player((sx, sy, sz + 24.0), (0.0, 0.0, 0.0))
    before = list(vm.fget_v(sv.player, _off(sv, "origin")))
    sv.player_carry = [0.0, 0.0, 0.0]
    vm.fset_v(plat, _off(sv, "velocity"), (0.0, 0.0, 100.0))
    sv._push_move(plat, 0.1)
    after = vm.fget_v(sv.player, _off(sv, "origin"))
    assert list(after) == before, f"a distant bystander was dragged ({before} -> {after})"
    assert sv.player_carry == [0.0, 0.0, 0.0]


def test_descending_pusher_crushes_a_pinned_player():
    """A roof/door closing onto a player who can't move out of its way fires the
    pusher's .blocked (door_blocked -> T_Damage), crushing them. SV_PushMove
    tests the shoved entity against the pusher itself (SV_TestEntityPosition);
    the port previously checked only world solid, so a player pinned between the
    floor and a descending brush was never registered as stuck and never crushed."""
    sv = _boot()
    vm = sv.vm
    sv.spawn_player((480.0, -352.0, 88.0), (0.0, 0.0, 0.0))   # on the spawn floor
    p = sv.player
    po = vm.fget_v(p, _off(sv, "origin"))
    # a door with a brush hull + .blocked (door_blocked, dmg 2); reposition its
    # brush to straddle the player's head, then drive it straight down
    door = 41
    vm.fset_v(door, _off(sv, "origin"),
              (po[0] - 544, po[1] - 2248, (po[2] + 30) + 144))
    sv._link_abs(door)
    assert sv._penetrates_pusher(p, door), "test setup: door should overlap player"
    vm.fset_v(door, _off(sv, "velocity"), (0.0, 0.0, -120.0))
    h0 = sv.player_health()
    for _ in range(8):
        sv.gset_f("time", sv.time)
        sv._push_move(door, 0.1)
    assert sv.player_health() < h0, \
        f"descending pusher did not crush the pinned player ({h0} -> {sv.player_health()})"


if __name__ == "__main__":
    test_door_push_never_leaves_player_out_of_bounds()
    test_lift_carries_a_rider_standing_on_top()
    test_mover_does_not_drag_a_clear_bystander()
    test_descending_pusher_crushes_a_pinned_player()
    print("OK")
