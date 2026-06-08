"""Regression test: double doors link, so both leaves open together.

Double doors auto-link at spawn: LinkDoors walks the door list and chains any
two whose bounding boxes touch (EntitiesTouching) via .enemy, and door_fire
walks that chain so opening one opens all. Key doors don't spawn a trigger
field, so they're opened by touching a leaf -- which still opens both via the
chain.

The leaves of a double door meet exactly in the middle, so their stored brush
bounds just abut (e.g. one ends at x=239, the other starts at x=241 around a
x=240 seam). Quake's Mod_LoadSubmodels spreads every submodel's mins/maxs by a
pixel (-1 / +1), which closes that seam so the boxes overlap and link. Without
that spread the boxes read as a 2-unit gap, EntitiesTouching fails, each leaf
becomes its own singleton, and only the side you touch opens.

Driven against the real shareware progs; e1m2 has a silver-key double door.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot(mapn):
    pak = Pak(PAK)
    b = Bsp(pak.read(f"maps/{mapn}.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname=f"maps/{mapn}.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    for _ in range(3):                  # let the LinkDoors think fire
        sv.run_frame(0.1)
    return sv


def _key_doors(sv):
    vm, fb = sv.vm, sv.pr.field_by_name
    cn, items = fb["classname"][1], fb["items"][1]
    return [n for n in range(1, vm.num_edicts)
            if not vm.free[n]
            and sv.pr.string(vm.fget_i(n, cn)) == "door"
            and vm.fget_f(n, items) != 0]


def test_key_double_doors_link():
    sv = _boot("e1m2")
    vm, fb = sv.vm, sv.pr.field_by_name
    enemy = fb["enemy"][1]
    doors = _key_doors(sv)
    assert len(doors) >= 2, f"expected a key double door on e1m2, found {doors}"
    for n in doors:
        assert vm.fget_i(n, enemy) not in (0, n), (
            f"key door {n} linked to itself (enemy={vm.fget_i(n, enemy)}) "
            "-- the pair never joined, so only one side opens")


def test_opening_one_leaf_opens_both():
    sv = _boot("e1m2")
    vm, fb = sv.vm, sv.pr.field_by_name
    off = lambda n: fb[n][1]
    a, b = _key_doors(sv)[:2]
    before = (vm.fget_v(a, off("origin")), vm.fget_v(b, off("origin")))
    # open one leaf the way door_touch does -> door_use -> door_fire walks .enemy
    sv.gset_f("time", sv.time)
    sv.gset_i("self", a)
    sv.gset_i("other", sv.player or 0)
    vm.execute(vm.fget_i(a, off("use")))
    for _ in range(6):
        sv.run_frame(0.1)
    after = (vm.fget_v(a, off("origin")), vm.fget_v(b, off("origin")))
    moved_a = after[0] != before[0]
    moved_b = after[1] != before[1]
    assert moved_a and moved_b, (
        f"opening one leaf didn't open both (a moved={moved_a}, b moved={moved_b})")


if __name__ == "__main__":
    test_key_double_doors_link()
    test_opening_one_leaf_opens_both()
    print("OK")
