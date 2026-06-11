"""Particle velocity integration (sv.py _advance_particles / _burst).

Quake's R_DrawParticles ramps explosion-particle velocity each frame (r_part.c
pt_explode / pt_explode2: vel += vel * 4*frametime), so an explosion bursts
outward fast with a lingering core, and seeds them at (rand%512)-256 = +/-256
u/s. The port previously drifted them at a flat +/-200 with no ramp, which read
as near-static. These tests pin the corrected integration.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server, _TE_EFFECT
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b, mapname="maps/e1m1.bsp",
                skill=1, physics=Physics(b), pak=pak)
    sv.load_level()
    return sv


def test_positive_accel_ramps_velocity_up():
    sv = _boot()
    # x velocity 100, accel +4/s: after 0.1s vel.x *= (1 + 4*0.1) = 1.4
    sv.particles[:] = [[0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 5, sv.time + 10, 4.0]]
    sv.time += 0.1
    sv._advance_particles(0.1)
    assert abs(sv.particles[0][3] - 140.0) < 1e-6, sv.particles[0][3]


def test_negative_accel_ramps_velocity_down():
    sv = _boot()
    # accel -4/s (pt_explode2): vel.x *= (1 - 4*0.1) = 0.6 -> 60
    sv.particles[:] = [[0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 5, sv.time + 10, -4.0]]
    sv.time += 0.1
    sv._advance_particles(0.1)
    assert abs(sv.particles[0][3] - 60.0) < 1e-6, sv.particles[0][3]


def test_zero_accel_keeps_constant_horizontal_velocity():
    sv = _boot()
    sv.particles[:] = [[0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 5, sv.time + 10, 0.0]]
    sv.time += 0.1
    sv._advance_particles(0.1)
    assert abs(sv.particles[0][3] - 100.0) < 1e-6, "no-accel particle changed speed"


def test_explosion_te_is_fast_and_accelerating():
    # the explosion temp-entity (type 3) seeds +/-256 and a non-zero outward ramp
    _color, _count, spread, accel = _TE_EFFECT[3]
    assert spread == 256, f"explosion spread should be 256, got {spread}"
    assert accel > 0, "explosion particles should accelerate outward"


def test_explosion_burst_outruns_constant_velocity():
    sv = _boot()
    sv.particles[:] = []
    sv._burst((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 75, 24, 256, 4.0)
    init_max = max((p[3] ** 2 + p[4] ** 2 + p[5] ** 2) ** 0.5 for p in sv.particles)
    for _ in range(10):                              # 0.5 s
        sv.time += 0.05
        sv._advance_particles(0.05)
    far = max((p[0] ** 2 + p[1] ** 2 + p[2] ** 2) ** 0.5 for p in sv.particles)
    # without acceleration the farthest a particle could get in 0.5s is
    # init_speed * 0.5; the ramp must carry the leading particles well past that
    assert far > init_max * 0.5 * 1.5, \
        f"explosion did not accelerate (reached {far:.0f}, flat bound {init_max*0.5:.0f})"


def test_trail_particles_do_not_accelerate():
    sv = _boot()
    sv.particles[:] = []
    sv._rocket_trail((0.0, 0.0, 0.0), (96.0, 0.0, 0.0), 0)   # a trail segment
    assert sv.particles, "no trail particles spawned"
    assert all(p[8] == 0.0 for p in sv.particles), "trail particles must not accelerate"


if __name__ == "__main__":
    test_positive_accel_ramps_velocity_up()
    test_negative_accel_ramps_velocity_down()
    test_zero_accel_keeps_constant_horizontal_velocity()
    test_explosion_te_is_fast_and_accelerating()
    test_explosion_burst_outruns_constant_velocity()
    test_trail_particles_do_not_accelerate()
    print("OK")
