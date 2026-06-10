"""Regression test: the player collides with SOLID_BBOX and SOLID_SLIDEBOX
entities -- misc_explobox barrels and monsters -- instead of walking through them.

Quake's SV_Move (world.c SV_ClipToLinks) clips a move against *every* solid edict
in the area, not just the SOLID_BSP brush movers (doors, func_walls). For a
SOLID_BBOX/SOLID_SLIDEBOX entity, SV_HullForEntity builds a temp box hull from the
entity's bounding box (Minkowski-expanded by the mover's box) and clips against it.
This port previously only gathered SOLID_BSP brush models, so the player passed
straight through barrels and monsters. This test pins the fixed behaviour.

Driven against the real shareware progs on e1m1.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"
FL_MONSTER = 32


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


def _find_classname(sv, name):
    vm, f = sv.vm, sv.f
    for num in range(1, vm.num_edicts):
        if vm.free[num]:
            continue
        if sv.pr.string(vm.fget_i(num, f["classname"])) == name:
            return num
    raise AssertionError(f"no {name} on e1m1")


def _find_monster(sv):
    vm, f = sv.vm, sv.f
    for num in range(1, vm.num_edicts):
        if vm.free[num] or num == sv.player:
            continue
        if int(vm.fget_f(num, f["flags"])) & FL_MONSTER:
            return num
    raise AssertionError("no live monster on e1m1")


def _wire_boxes(sv):
    """Mirror what the host frontend does each frame: feed physics the solid box
    entities (barrels, monsters) the player should collide with."""
    sv.phys.set_box_entities(sv.solid_box_entities(ignore=sv.player))


def _move_through(sv, ent):
    """Trace the player origin straight along +x through `ent`'s box centre,
    starting and ending well clear of it. Returns the Trace."""
    vm, f = sv.vm, sv.f
    amn = vm.fget_v(ent, f["absmin"])
    amx = vm.fget_v(ent, f["absmax"])
    cy = (amn[1] + amx[1]) * 0.5
    cz = (amn[2] + amx[2]) * 0.5
    start = (amn[0] - 64.0, cy, cz)
    end = (amx[0] + 64.0, cy, cz)
    return sv.phys.move(list(start), list(end)), amn, amx


def test_explobox_blocks_the_player():
    sv = _boot()
    ex = _find_classname(sv, "misc_explobox")
    _wire_boxes(sv)
    tr, amn, amx = _move_through(sv, ex)
    assert not tr.startsolid, "player should start clear of the barrel"
    assert tr.fraction < 1.0, "player walked straight through the barrel"
    assert tr.ent == ex, f"barrel should be the blocking entity (got {tr.ent})"
    # stopped on the near (-x) side, not tunnelled out the far side
    assert tr.endpos[0] < amn[0], f"player tunnelled into the barrel: {tr.endpos}"


def test_monster_blocks_the_player():
    sv = _boot()
    mon = _find_monster(sv)
    _wire_boxes(sv)
    tr, amn, amx = _move_through(sv, mon)
    assert not tr.startsolid, "player should start clear of the monster"
    assert tr.fraction < 1.0, "player walked straight through the monster"
    assert tr.ent == mon, f"monster should be the blocking entity (got {tr.ent})"
    assert tr.endpos[0] < amn[0], f"player tunnelled into the monster: {tr.endpos}"


def test_box_list_is_what_blocks():
    """Control: with no box entities wired, the same through-the-barrel move is
    unobstructed -- proving it's the box clip (not world geometry) that blocks,
    and that the player isn't spuriously stuck in the level here."""
    sv = _boot()
    ex = _find_classname(sv, "misc_explobox")
    sv.phys.set_box_entities([])
    tr, _amn, _amx = _move_through(sv, ex)
    assert tr.fraction == 1.0, "world geometry blocked the control move; pick another spot"


def test_player_excluded_from_own_clip():
    """The player is itself SOLID_SLIDEBOX; it must not be in the clip list it
    moves against, or every move would start solid."""
    sv = _boot()
    boxes = sv.solid_box_entities(ignore=sv.player)
    assert all(ent != sv.player for _mn, _mx, ent in boxes), \
        "player must be excluded from its own box-clip list"


def test_monster_probes_skip_box_clip():
    """Monster locomotion traces (record=False) keep the existing single-hull
    behaviour -- box clipping is the player path only -- so wiring barrels in
    doesn't silently change monster pathing."""
    sv = _boot()
    ex = _find_classname(sv, "misc_explobox")
    _wire_boxes(sv)
    vm, f = sv.vm, sv.f
    amn = vm.fget_v(ex, f["absmin"]); amx = vm.fget_v(ex, f["absmax"])
    cy = (amn[1] + amx[1]) * 0.5; cz = (amn[2] + amx[2]) * 0.5
    start = (amn[0] - 64.0, cy, cz); end = (amx[0] + 64.0, cy, cz)
    tr = sv.phys.move(list(start), list(end), record=False)
    assert tr.fraction == 1.0, "monster probe should ignore box entities"


if __name__ == "__main__":
    test_explobox_blocks_the_player()
    test_monster_blocks_the_player()
    test_box_list_is_what_blocks()
    test_player_excluded_from_own_clip()
    test_monster_probes_skip_box_clip()
    print("OK")
