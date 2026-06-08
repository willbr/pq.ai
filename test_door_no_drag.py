"""Regression test: touching a door must not drag the player into the walls.

A pusher (door/lift) carries the player when they RIDE it. The ride test was
just "player box overlaps the pusher box", so standing in front of a door --
whose brush spans floor to ceiling -- counted as riding. When the door slid open
it shifted the player by its full travel, hauling them through the wall and out
of the level.

Quake only carries an entity that is standing on the pusher (a lift) or that the
pusher moves into (and then a block test stops it). Riding is now "feet on top
of the pusher", so a lift still carries the player up while a door sliding past
them does not.

Driven against the real shareware progs on e1m1 (door 7 is a tall sliding door).
"""

from pak import Pak
from bsp import Bsp
from progs import Progs
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
    for _ in range(3):
        sv.run_frame(0.1)
    return sv


def test_sliding_door_does_not_drag_player():
    sv = _boot()
    vm, fb = sv.vm, sv.pr.field_by_name
    off = lambda n: fb[n][1]
    # door 7 spans roughly x209-255 y513-615 z1-127; stand the player inside that
    # footprint with feet on the floor (well below the door top, so NOT on top).
    door = 7
    mn = vm.fget_v(door, off("mins")); mx = vm.fget_v(door, off("maxs"))
    cx = (mn[0] + mx[0]) * 0.5
    cy = (mn[1] + mx[1]) * 0.5
    feet_z = mn[2] + 24.0                       # player origin: feet near door base
    sv.spawn_player((cx, cy, feet_z), (0.0, 0.0, 0.0))
    start = list(vm.fget_v(sv.player, off("origin")))

    # open the door and let it travel
    sv.gset_f("time", sv.time); sv.gset_i("self", door); sv.gset_i("other", sv.player)
    vm.execute(vm.fget_i(door, off("use")))
    moved = 0.0
    for _ in range(8):
        sv.run_frame(0.1)
        now = vm.fget_v(sv.player, off("origin"))
        moved = max(moved, abs(now[0] - start[0]), abs(now[1] - start[1]),
                    abs(now[2] - start[2]))
    assert moved < 8.0, (
        f"player was dragged {moved:.1f}u by a door they only stood next to "
        "(should stay put)")


if __name__ == "__main__":
    test_sliding_door_does_not_drag_player()
    print("OK")
