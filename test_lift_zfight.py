"""Coplanar lift/wall z-fighting, fixed structurally by the span/edge renderer.

A moving brush model (the e1m1 lift at 552,2032,-168) sits flush with the wall it
slides across. The old per-pixel float z-buffer resolved both faces to near-equal
1/z, so which one won flickered pixel-to-pixel and frame-to-frame -- z-fighting.
The span/edge renderer (quake/r_edge.py) resolves occlusion once per span via the
surface stack, breaking coplanar ties deterministically (id's R_LeadingEdge key /
1%-fudge logic, r_edge.c:482). So identical inputs give byte-identical output --
no flicker --
and a sub-pixel camera nudge changes only ordinary edge pixels, not a large
z-fighting region.
"""

import os

os.environ.setdefault("PQ_AUDIO", "0")

from client import Client, InputState


def _boot_near_lift():
    c = Client("e1m1")
    c.resize(320, 240)
    c.pos = [552.0, 2032.0 + 120, -168.0 + 40]      # stand back of/above the lift
    c.yaw = 270.0
    c.pitch = 10.0
    for _ in range(3):
        c.sv.update_player((c.pos[0], c.pos[1], c.pos[2]), (c.pitch, c.yaw, 0.0))
        c.frame(0.05, InputState(mouselook=True))
    return c


def test_fixed_camera_is_byte_identical():
    # The direct z-fight signal: at a fixed camera, two renders must be identical.
    # The float z-buffer flickered here; the surface-stack tie-break does not.
    c = _boot_near_lift()
    a = bytes(c.frame(0.0, InputState(mouselook=True)).framebuffer[0])
    b = bytes(c.frame(0.0, InputState(mouselook=True)).framebuffer[0])
    flips = sum(1 for x, y in zip(a, b) if x != y)
    assert flips == 0, f"lift region flickers at a fixed camera: {flips} pixels"


def test_subpixel_move_changes_only_edges():
    # A sub-pixel camera nudge over the coplanar lift/wall seam must change only a
    # small number of edge pixels -- not the large region (~2100 on a 320x240
    # frame) the float z-buffer z-fought. ~465 here is ordinary edge change.
    c = _boot_near_lift()
    frames = []
    for dpos in (0.0, 0.03):
        c.pos[1] = 2032.0 + 120 + dpos
        frames.append(bytes(c.frame(0.0, InputState(mouselook=True)).framebuffer[0]))
    flips = sum(1 for a, b in zip(frames[0], frames[1]) if a != b)
    assert flips < 1000, f"coplanar lift/wall region looks unstable: {flips} flips"


if __name__ == "__main__":
    test_fixed_camera_is_byte_identical()
    test_subpixel_move_changes_only_edges()
    print("OK")
