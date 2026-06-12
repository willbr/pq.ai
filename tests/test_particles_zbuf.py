"""Particles in the textured z-buffer renderer (D_DrawParticle, d_part.c).

The flat/wire renderers have no depth buffer, so particles (teleport fog,
rocket/blood trails, explosions) are projected to screen and drawn as an overlay
occluded by a coarse per-particle line-of-sight trace. The textured renderer has
a real z-buffer, so it rasterises each particle straight into the framebuffer as
a small distance-scaled square with the per-pixel depth test -- previously it
drew none at all, the reported fault. These tests drive the real Client on e1m1.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from client import Client, InputState
from quake.render import angle_vectors

COLOR = 254              # a bright index we count in the framebuffer


def _boot():
    c = Client("e1m1")
    c.resize(320, 240)
    return c


def _eye_and_forward(c):
    eye = (c.pos[0], c.pos[1], c.pos[2] + 22.0)
    fwd, _r, _u = angle_vectors(c.yaw, c.pitch)
    return eye, fwd


def _fb_color_count(c):
    rf = c.frame(0.0, InputState())
    return rf.framebuffer[0].count(COLOR), rf


def test_particle_rasterised_into_framebuffer():
    c = _boot()
    eye, fwd = _eye_and_forward(c)
    c.sv.particles[:] = []
    base, rf = _fb_color_count(c)
    assert rf.mode == "zbuf"
    # a particle 40 units ahead, in clear line of sight
    pt = (eye[0] + fwd[0] * 40, eye[1] + fwd[1] * 40, eye[2] + fwd[2] * 40)
    assert c.phys.trace(list(eye), list(pt)).fraction == 1.0, "test spot is occluded"
    c.sv.particles[:] = [[pt[0], pt[1], pt[2], 0, 0, 0, COLOR, 99]]
    front, rf = _fb_color_count(c)
    assert front - base > 10, \
        f"particle not drawn into the framebuffer (delta {front - base})"
    # and it is NOT also emitted as an overlay sprite (no double draw)
    assert rf.particles == [], "zbuf mode must not also overlay the particle"


def test_particle_occluded_by_wall_per_pixel():
    c = _boot()
    eye, fwd = _eye_and_forward(c)
    c.sv.particles[:] = []
    base, _ = _fb_color_count(c)
    # find the nearest wall straight ahead, drop the particle just behind it
    far = (eye[0] + fwd[0] * 4000, eye[1] + fwd[1] * 4000, eye[2] + fwd[2] * 4000)
    wd = c.phys.trace(list(eye), list(far)).fraction * 4000
    behind = (eye[0] + fwd[0] * (wd + 100), eye[1] + fwd[1] * (wd + 100),
              eye[2] + fwd[2] * (wd + 100))
    c.sv.particles[:] = [[behind[0], behind[1], behind[2], 0, 0, 0, COLOR, 99]]
    back, _ = _fb_color_count(c)
    assert back - base <= 3, \
        f"particle behind a wall not occluded by the z-buffer (delta {back - base})"


def test_flat_mode_keeps_the_overlay():
    c = _boot()
    eye, fwd = _eye_and_forward(c)
    pt = (eye[0] + fwd[0] * 40, eye[1] + fwd[1] * 40, eye[2] + fwd[2] * 40)
    c.sv.particles[:] = [[pt[0], pt[1], pt[2], 0, 0, 0, COLOR, 99]]
    c.mode = "flat"                          # depthless: particles are an overlay
    rf = c.frame(0.0, InputState())
    assert rf.framebuffer is None
    assert len(rf.particles) == 1, "flat mode should still overlay the particle"


if __name__ == "__main__":
    test_particle_rasterised_into_framebuffer()
    test_particle_occluded_by_wall_per_pixel()
    test_flat_mode_keeps_the_overlay()
    print("OK")
