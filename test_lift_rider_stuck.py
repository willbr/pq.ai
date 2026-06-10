"""Regression test: a rider must not be left embedded in a pusher (lift/door)
that finishes its move partway through a frame.

A MOVETYPE_PUSH mover advances at constant velocity and carries its rider
(SV_PushMove). Quake's SV_Physics_Pusher advances it by only
`movetime = nextthink - ltime` on the frame the move completes, so the mover
lands exactly on its destination and the rider is carried by exactly that much;
the mover's think (SUB_CalcMoveDone) then snaps it to the destination and zeroes
its velocity.

The bug: advancing the mover by the *full* frame instead overshoots the
destination and carries the rider to the overshoot, then the think snaps the
mover back -- stranding the rider below its surface, embedded in it. An embedded
player is `allsolid`, so every move (including a jump) is zeroed: you're stuck
until you noclip.

Reproduced end-to-end on e1m1: button *4 triggers the func_door lift *3
(angle -2, descends). Stand on it, press the button, ride to the bottom -- and
without the fix you end up wedged in the lift, unable to walk or jump out.
"""

import math
from client import Client, InputState

PAK = "quake-shareware/id1/pak0.pak"


def _lift_edict(cl, sub):
    vm, f, mp = cl.sv.vm, cl.sv.f, cl.sv.model_precache
    for n in range(1, vm.num_edicts):
        if vm.free[n]:
            continue
        mi = vm.fget_i(n, f["modelindex"])
        if 0 < mi < len(mp) and mp[mi] == "*" + str(sub):
            return n
    raise AssertionError(f"no edict drives submodel *{sub}")


def test_ride_lift_to_bottom_then_walk_off():
    cl = Client("e1m1")
    vm, f = cl.sv.vm, cl.sv.f
    for _ in range(10):
        cl.frame(0.1, InputState())
    lift = _lift_edict(cl, 3)
    lift_top_local = cl.sv.bsp.models[3]["maxs"][2]

    def lift_top():
        return lift_top_local + vm.fget_v(lift, f["origin"])[2]

    # stand on the lift's top surface, facing the button (-x), and press it
    cl.pos = [-30.0, 574.0, lift_top() + 24.0]
    cl.vel = [0.0, 0.0, 0.0]
    cl.onground = True
    cl.yaw, cl.pitch = 180.0, 0.0
    for i in range(3):
        cl.frame(0.1, InputState(move_forward=1.0))   # walk into / press button

    # ride down: stand still until the lift has stopped at the bottom
    worst_gap = 0.0
    for _ in range(40):
        cl.frame(0.1, InputState())
        if vm.fget_v(lift, f["velocity"])[2] == 0.0:
            worst_gap = min(worst_gap, (cl.pos[2] - 24.0) - lift_top())

    assert worst_gap > -1.0, (
        f"rider embedded {(-worst_gap):.1f} units into the stopped lift -- "
        "the stuck-on-lift bug")

    # now try to walk off (and jump) -- a stuck/embedded player can't move at all
    cl.yaw = 0.0
    start = list(cl.pos)
    for _ in range(15):
        cl.frame(0.1, InputState(move_forward=1.0, move_up=1.0))
    moved = math.dist(cl.pos, start)
    assert moved > 16.0, f"player only moved {moved:.1f} units -- stuck on the lift/button"


if __name__ == "__main__":
    test_ride_lift_to_bottom_then_walk_off()
    print("OK")
