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
    entities (barrels, monsters, the player) and point passent at the player, so
    the player's move clips against the others but skips itself."""
    sv.phys.passent = sv.player
    sv.phys.set_box_entities(sv.solid_box_entities())


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


def test_player_in_list_but_skipped_via_passent():
    """The player is itself SOLID_SLIDEBOX, so it IS in the clip list (monsters
    must be able to clip against it) -- but its own move skips it via passent,
    or every move would start solid. SV_ClipToLinks' passedict, world.c:849."""
    sv = _boot()
    boxes = sv.solid_box_entities()
    assert any(ent == sv.player for _mn, _mx, ent, _own in boxes), \
        "player must be in the box list so monsters can clip against it"
    # the player's own forward move from its current spot must not start solid
    sv.phys.passent = sv.player
    sv.phys.set_box_entities(boxes)
    org = sv.vm.fget_v(sv.player, sv.f["origin"])
    tr = sv.phys.move(list(org), [org[0] + 16.0, org[1], org[2]])
    assert not tr.startsolid, "player clipped against itself (passent not honoured)"


def test_monster_probe_clips_box_but_stays_silent():
    """Monster locomotion traces (record=False) now DO clip against box solids --
    a monster can't walk through the player or a barrel (world.c SV_ClipToLinks
    runs for every move) -- but they skip the mover itself and don't record
    touches meant for the player."""
    sv = _boot()
    ex = _find_classname(sv, "misc_explobox")
    mon = _find_monster(sv)
    _wire_boxes(sv)
    sv.phys.touched.clear()
    vm, f = sv.vm, sv.f
    amn = vm.fget_v(ex, f["absmin"]); amx = vm.fget_v(ex, f["absmax"])
    cy = (amn[1] + amx[1]) * 0.5; cz = (amn[2] + amx[2]) * 0.5
    start = (amn[0] - 64.0, cy, cz); end = (amx[0] + 64.0, cy, cz)
    # a monster probe (passedict=the monster) is blocked by the barrel...
    tr = sv.phys.move(list(start), list(end), record=False, passedict=mon)
    assert tr.fraction < 1.0 and tr.ent == ex, \
        "monster probe should clip against the barrel"
    # ...but records no touch (record=False)
    assert not sv.phys.touched, "monster probe must not record player touches"
    # and a probe that starts on the mover itself skips it (no self-collision)
    mam = vm.fget_v(mon, f["absmin"]); max_ = vm.fget_v(mon, f["absmax"])
    mcy = (mam[1] + max_[1]) * 0.5; mcz = (mam[2] + max_[2]) * 0.5
    selftr = sv.phys.move([mam[0] - 64.0, mcy, mcz], [max_[0] + 64.0, mcy, mcz],
                          record=False, passedict=mon)
    assert selftr.ent != mon, "monster probe must not clip against itself"


def test_monster_blocked_by_player():
    """Fault 3: a monster walking into the player is stopped by the player's box,
    instead of interpenetrating it (which left both stuck). The monster move
    clips against the player edict, which is in the box list."""
    sv = _boot()
    mon = _find_monster(sv)
    _wire_boxes(sv)
    vm, f = sv.vm, sv.f
    pmn = vm.fget_v(sv.player, f["absmin"]); pmx = vm.fget_v(sv.player, f["absmax"])
    pcy = (pmn[1] + pmx[1]) * 0.5; pcz = (pmn[2] + pmx[2]) * 0.5
    start = (pmn[0] - 64.0, pcy, pcz); end = (pmx[0] + 64.0, pcy, pcz)
    tr = sv.phys.move(list(start), list(end), record=False, passedict=mon)
    assert tr.fraction < 1.0 and tr.ent == sv.player, \
        "monster walked straight through the player (fault 3)"


def test_player_skips_own_nails():
    """Fault 4: the player's move ignores SOLID_BBOX entities it owns -- the nails
    the nailgun spawns at the muzzle -- so firing while walking doesn't trip over
    them. SV_ClipToLinks skips touch->owner == passedict (world.c:851)."""
    sv = _boot()
    _wire_boxes(sv)
    vm, f = sv.vm, sv.f
    org = vm.fget_v(sv.player, f["origin"])
    # a player-owned box solid sitting just ahead, like a freshly launched nail
    nail = (org[0] + 8.0, org[1], org[2])
    boxes = list(sv.solid_box_entities())
    fake = ([nail[0] - 1, nail[1] - 1, nail[2] - 1],
            [nail[0] + 1, nail[1] + 1, nail[2] + 1], 99999, sv.player)
    boxes.append(fake)
    sv.phys.passent = sv.player
    sv.phys.set_box_entities(boxes)
    tr = sv.phys.move(list(org), [org[0] + 32.0, org[1], org[2]], passedict=sv.player)
    assert tr.fraction == 1.0 or tr.ent != 99999, \
        "player clipped against its own nail (fault 4)"


if __name__ == "__main__":
    test_explobox_blocks_the_player()
    test_monster_blocks_the_player()
    test_box_list_is_what_blocks()
    test_player_in_list_but_skipped_via_passent()
    test_monster_probe_clips_box_but_stays_silent()
    test_monster_blocked_by_player()
    test_player_skips_own_nails()
    print("OK")
