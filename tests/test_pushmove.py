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

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

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


def test_lift_carries_a_dead_body_on_top():
    """A monster corpse (SOLID_NOT) resting on a lift must ride it up, like in
    real Quake. WinQuake's SV_PushMove has no early SOLID_NOT skip: an entity
    standing on the pusher (FL_ONGROUND && groundentity == pusher) is always
    carried; SOLID_NOT is only special-cased later, in the block path. The port
    had added a top-of-loop `solid == SOLID_NOT: continue`, so dead bodies were
    left behind by the lift."""
    sv = _boot()
    vm = sv.vm
    o = lambda n: _off(sv, n)
    plat = _a_plat(sv)
    amn = vm.fget_v(plat, o("absmin")); amx = vm.fget_v(plat, o("absmax"))
    cx = (amn[0] + amx[0]) * 0.5; cy = (amn[1] + amx[1]) * 0.5
    # a soldier corpse sitting on the plat's top
    corpse = vm.alloc_edict()
    vm.fset_f(corpse, o("movetype"), 4.0)        # MOVETYPE_STEP (as monsters)
    vm.fset_f(corpse, o("solid"), 0.0)           # SOLID_NOT (dead body)
    vm.fset_v(corpse, o("mins"), (-16.0, -16.0, -24.0))
    vm.fset_v(corpse, o("maxs"), (16.0, 16.0, 40.0))
    vm.fset_v(corpse, o("origin"), (cx, cy, amx[2] + 24.0))
    sv._link_abs(corpse)
    vm.fset_f(corpse, o("flags"), 512.0)         # FL_ONGROUND
    vm.fset_i(corpse, o("groundentity"), plat)
    z0 = vm.fget_v(corpse, o("origin"))[2]
    vm.fset_v(plat, o("velocity"), (0.0, 0.0, 100.0))   # rise
    sv._push_move(plat, 0.1)
    z1 = vm.fget_v(corpse, o("origin"))[2]
    assert abs((z1 - z0) - 10.0) < 0.5, f"dead body not carried up with the lift ({z1 - z0})"


def test_rising_lift_carries_a_walking_monster():
    """A monster standing on a rising lift must ride it up even while its own AI
    runs a movestep that same frame. WinQuake relinks the pusher (SV_LinkEdict)
    the instant it moves inside SV_PushMove, so a monster re-grounding afterwards
    (SV_movestep's drop-to-floor SV_Move) lands on the lift's NEW top. The port
    caches brush-mover positions per host frame; without refreshing the moved
    mover's cache entry, the monster's movestep traced against the lift's STALE
    (lower) position and re-grounded there, dropping off the rising lift -- the
    e1m4 "ogre stuck underneath the lift" bug, nondeterministic because it only
    bit when the ogre's random-walk AI happened to step that frame.

    Driven on e1m4, where ogre 90 spawns standing on func_door lift 89."""
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m4.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname="maps/e1m4.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    vm = sv.vm
    o = lambda n: _off(sv, n)

    def snapshot():
        # what the host (client.py) does before each server frame
        sv.phys.set_brush_entities(sv.solid_brush_models())
        sv.phys.set_box_entities(sv.solid_box_entities())

    for _ in range(3):                      # let the ogre droptofloor onto the lift
        snapshot()
        sv.run_frame(0.1)

    lift, ogre = 89, 90
    assert sv.pr.string(vm.fget_i(ogre, o("classname"))) == "monster_ogre", \
        "test fixture moved: e1m4 edict 90 is no longer the ogre on the lift"
    assert vm.fget_i(ogre, o("groundentity")) == lift, \
        "test setup: the ogre should settle standing on the lift"

    # raise the lift and, every frame, drive a movestep the way the awake ogre's
    # AI would -- the exact interleave (mover moves, then monster re-grounds) that
    # exposed the stale cache.
    vm.fset_v(lift, o("velocity"), (0.0, 0.0, 100.0))
    for _ in range(7):
        snapshot()
        sv.time += 0.1
        sv.gset_f("time", sv.time)
        vm.time = sv.time
        sv._push_move(lift, 0.1)            # lift rises, carrying the ogre
        sv.gset_i("self", ogre)
        sv._sv_movestep(ogre, (8.0, 0.0, 0.0), relink=True)   # AI step + re-ground
        sv._sv_movestep(ogre, (-8.0, 0.0, 0.0), relink=True)  # step back: stay put

    top = vm.fget_v(lift, o("absmax"))[2]
    feet = vm.fget_v(ogre, o("absmin"))[2]
    assert abs(feet - top) < 4.0, \
        f"the rising lift left the walking monster behind (feet {feet} vs top {top})"
    assert vm.fget_i(ogre, o("groundentity")) == lift, \
        "the monster lost its footing on the lift"


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


def test_descending_pusher_does_not_shove_player_down_through_gaps():
    """A roof closing on a player with open space below (standing on a high step,
    a staircase) must crush them where they are, not shove them DOWN into the gap
    -- SV_TestEntityPosition tests against the pusher, so a player still inside
    the descending brush is blocked even when the spot below isn't world solid.
    Checking only world solid pushed the player down through the stairs."""
    sv = _boot()
    vm = sv.vm
    # float the player above the floor so a downward shove doesn't immediately
    # hit world solid (the staircase case)
    sv.spawn_player((480.0, -352.0, 120.0), (0.0, 0.0, 0.0))
    p = sv.player
    po = vm.fget_v(p, _off(sv, "origin"))
    z0 = po[2]
    door = 41
    vm.fset_v(door, _off(sv, "origin"),
              (po[0] - 544, po[1] - 2248, (po[2] + 30) + 144))
    sv._link_abs(door)
    vm.fset_v(door, _off(sv, "velocity"), (0.0, 0.0, -100.0))
    h0 = sv.player_health()
    for _ in range(6):
        sv.gset_f("time", sv.time)
        sv._push_move(door, 0.1)
    z1 = vm.fget_v(p, _off(sv, "origin"))[2]
    assert z1 >= z0 - 1.0, f"roof shoved the player down through the gap ({z0} -> {z1})"
    assert sv.player_health() < h0, "player not crushed (was shoved down instead)"


if __name__ == "__main__":
    test_door_push_never_leaves_player_out_of_bounds()
    test_lift_carries_a_rider_standing_on_top()
    test_lift_carries_a_dead_body_on_top()
    test_rising_lift_carries_a_walking_monster()
    test_mover_does_not_drag_a_clear_bystander()
    test_descending_pusher_crushes_a_pinned_player()
    test_descending_pusher_does_not_shove_player_down_through_gaps()
    print("OK")
