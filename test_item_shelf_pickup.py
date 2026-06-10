"""Regression: items flush against a wall (on a shelf) must be collectable.

Bug: the nail ammo on e1m1 sits on a shelf at origin (272,2352,64); its trigger
box's west face (x=272) is flush with the solid shelf wall. The player can never
get their bounding box past x=272 -- movement is clipped a DIST_EPSILON fraction
short of any wall -- so jumping up against the shelf leaves absmax.x ~= 271.97,
0.03 units short of the trigger. Our _link_abs set absmin/absmax exactly, so the
AABB overlap test never fired and the ammo was uncollectable.

Quake's SV_LinkEdict (world.c) expands the abs box: FL_ITEM bonus items by 15 on
x/y ("to make items easier to pick up and allow them to be grabbed off of
shelves"), everything else by 1 on all axes ("because movement is clipped an
epsilon away from an actual edge"). _link_abs now does the same.

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


def _find_shelf_spikes(sv):
    """The item_spikes resting on the shelf at ~(272,2352,64)."""
    vm, f = sv.vm, sv.f
    best, bestd = None, 1e30
    for num in range(1, vm.num_edicts):
        if vm.free[num]:
            continue
        if sv.pr.string(vm.fget_i(num, f["classname"])) != "item_spikes":
            continue
        ox, oy, oz = vm.fget_v(num, f["origin"])
        d = (ox - 272) ** 2 + (oy - 2352) ** 2 + (oz - 64) ** 2
        if d < bestd:
            best, bestd = num, d
    return best


def test_item_abs_box_is_widened():
    """FL_ITEM entities get the +/-15 horizontal expansion SV_LinkEdict applies."""
    sv = _boot()
    for _ in range(5):
        sv.run_frame(0.1)
    vm, f = sv.vm, sv.f
    e = _find_shelf_spikes(sv)
    assert e is not None, "no item_spikes near the shelf on e1m1"
    ox, oy, oz = vm.fget_v(e, f["origin"])
    mn = vm.fget_v(e, f["mins"])
    mx = vm.fget_v(e, f["maxs"])
    amn = vm.fget_v(e, f["absmin"])
    amx = vm.fget_v(e, f["absmax"])
    # x/y widened by 15, z left at origin+mins/maxs
    assert abs(amn[0] - (ox + mn[0] - 15)) < 1e-3, (amn, ox, mn)
    assert abs(amx[0] - (ox + mx[0] + 15)) < 1e-3, (amx, ox, mx)
    assert abs(amn[1] - (oy + mn[1] - 15)) < 1e-3
    assert abs(amx[1] - (oy + mx[1] + 15)) < 1e-3
    assert abs(amn[2] - (oz + mn[2])) < 1e-3
    assert abs(amx[2] - (oz + mx[2])) < 1e-3


def test_player_collects_shelf_ammo_at_epsilon_gap():
    """Player pressed against the shelf face (absmax.x ~0.03 short of the trigger,
    as real physics leaves it) and jumped to z-overlap must collect the nails."""
    sv = _boot()
    for _ in range(5):
        sv.run_frame(0.1)
    vm, f = sv.vm, sv.f
    e = _find_shelf_spikes(sv)
    p = sv.player
    assert vm.fget_f(p, f["ammo_nails"]) == 0.0

    # Trigger west face is at x=272 (flush with the shelf wall). Real movement
    # leaves the player's east face a DIST_EPSILON fraction short: absmax.x ~271.97.
    # Player maxs.x = 16, so origin.x = 271.969 - 16. y centred in the box, z
    # raised so the player box (origin +/- (24..32) in z) overlaps the trigger.
    vm.fset_v(p, f["origin"], (271.969 - 16.0, 2368.0, 90.0))
    sv._link_abs(p)
    sv.touch_triggers(p)

    assert vm.fget_f(p, f["ammo_nails"]) > 0.0, \
        "shelf nail ammo not collected at the realistic epsilon gap"


if __name__ == "__main__":
    test_item_abs_box_is_widened()
    test_player_collects_shelf_ammo_at_epsilon_gap()
    print("OK")
