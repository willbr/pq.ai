"""Regression test: items survive droptofloor and stay in the world.

Bug: pickups (armor, ammo, health, weapons) vanished a couple of seconds after
the level loaded. items.qc's PlaceItem calls droptofloor() and remove()s the
item if it returns false ("fell out of level"). droptofloor traces the entity's
box 256 units down to find the floor.

The box trace (physics.move) always traced Quake's hull 1 *as if the moving box
were the player* (origin-relative mins (-16,-16,-24)), applying no hull offset.
The player box matches hull 1 exactly, so the player moved fine -- but items rest
on the floor with mins.z = 0 (origin at floor level, not 24u above it). Tracing
them as the player pushed their effective box 24u into the floor, so every floor
trace came back startsolid/allsolid -> droptofloor returned 0 -> the item was
removed. The fix applies Quake SV_HullForEntity's offset = hull.clip_mins - mins.

Driven against the real shareware progs on e1m1.
"""

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
                mapname="maps/e1m1.bsp", skill=1)
    sv.phys = Physics(b)
    sv.load_level()
    sv.spawn_player((480.0, -352.0, 88.0), (0.0, 0.0, 0.0))
    return sv


def test_items_survive_droptofloor():
    sv = _boot()
    before_alias = len(sv.alias_entities())
    before_bsp = len(sv.bsp_model_entities())
    # droptofloor runs in each item's first think; let a couple of seconds elapse
    for _ in range(20):
        sv.run_frame(0.1)
    after_alias = len(sv.alias_entities())
    after_bsp = len(sv.bsp_model_entities())
    # essentially nothing should be culled -- a stray item or two genuinely placed
    # in an awkward spot is tolerable, a wholesale collapse is the bug.
    assert after_alias >= before_alias - 1, (before_alias, after_alias)
    assert after_bsp >= before_bsp - 1, (before_bsp, after_bsp)


def test_armor_box_finds_floor():
    """The specific failing case: the item_armor1 at (688,480,80) sits in the
    open; its downward box trace must find the floor, not report allsolid."""
    sv = _boot()
    vm, f = sv.vm, sv.f
    for num in range(1, vm.num_edicts):
        if vm.free[num]:
            continue
        cn = sv.pr.string(vm.fget_i(num, f["classname"]))
        if cn != "item_armor1":
            continue
        org = vm.fget_v(num, f["origin"])
        mins = vm.fget_v(num, f["mins"])
        end = (org[0], org[1], org[2] - 256.0)
        tr = sv.phys.move(list(org), list(end), record=False, mins=mins)
        assert not tr.allsolid, f"{cn} at {org}: allsolid"
        assert tr.fraction < 1.0, f"{cn} at {org}: no floor (fraction 1.0)"
        return
    raise AssertionError("no item_armor1 found on e1m1")


if __name__ == "__main__":
    test_armor_box_finds_floor()
    test_items_survive_droptofloor()
    print("OK")
