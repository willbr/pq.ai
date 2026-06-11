"""Brush-model depth bias (render.py BMODEL_ZSCALE): stop coplanar lift/door
faces z-fighting the world in the textured z-buffer renderer.

A moving brush model (the e1m1 lift at 552,2032,-168) sits flush with the wall
it slides across. In the float z-buffer both faces resolve to near-equal 1/z, so
which one wins flickers pixel-to-pixel and frame-to-frame -- z-fighting. WinQuake
doesn't have this: its span renderer breaks coplanar ties categorically in the
bmodel's favour (r_edge.c:357). The port biases the bmodel depth by a hair so it
wins the tie deterministically (the documented stopgap; the span renderer is the
eventual structural fix).

The test measures z-fight instability directly: between two camera positions a
sub-pixel apart, a z-fighting region flips a large number of pixels. The bias
must cut that sharply.
"""

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
import quake.render as R
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


def _flip_count(c, bias):
    """Pixels that change between two sub-pixel-apart camera positions: high when
    coplanar faces z-fight, low when the depth tie is resolved."""
    R.BMODEL_ZSCALE = bias
    frames = []
    for dpos in (0.0, 0.03):
        c.pos[1] = 2032.0 + 120 + dpos
        frames.append(bytes(c.frame(0.0, InputState(mouselook=True)).framebuffer[0]))
    return sum(1 for a, b in zip(frames[0], frames[1]) if a != b)


def test_bias_suppresses_lift_zfighting():
    c = _boot_near_lift()
    try:
        unbiased = _flip_count(c, 1.0)
        biased = _flip_count(c, 1.001)
    finally:
        R.BMODEL_ZSCALE = 1.001
    # the depth tie was flipping a large region; the bias must cut it by well
    # over half (here ~2100 -> ~465, the residual being ordinary edge change
    # from the camera move, not z-fighting)
    assert biased < unbiased * 0.5, \
        f"bias did not suppress lift z-fighting: {unbiased} -> {biased} flips"


if __name__ == "__main__":
    test_bias_suppresses_lift_zfighting()
    print("OK")
