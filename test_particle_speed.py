"""Per-frame particle integration (sv.py _advance_particles == r_part.c
R_DrawParticles).

id branches on each particle's ptype every frame (r_part.c:734):
  pt_static    no gravity (tracers, voor trail)
  pt_fire      vel.z += grav (rises), cools through ramp3, dies at ramp >= 6
  pt_explode   vel += vel*dvel (dvel = 4*frametime), vel.z -= grav, ramp1
  pt_explode2  vel -= vel*frametime (1x decel, NOT 4x), vel.z -= grav, ramp2
  pt_blob      vel += vel*dvel, vel.z -= grav
  pt_blob2     vel.xy -= vel.xy*dvel, vel.z -= grav
  pt_grav/slowgrav  vel.z -= grav (falls)
with grav = frametime * sv_gravity.value * 0.05. These tests pin that switch.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import (Server, PT_STATIC, PT_FIRE, PT_GRAV, PT_SLOWGRAV,
                      PT_EXPLODE, PT_EXPLODE2, PT_BLOB, PT_BLOB2, _RAMP3)
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b, mapname="maps/e1m1.bsp",
                skill=1, physics=Physics(b), pak=pak)
    sv.load_level()
    return sv


def _one(sv, vel, ptype, ramp=0.0, color=0):
    """Advance a single particle one 0.1s frame and return it."""
    sv.particles[:] = [[0.0, 0.0, 0.0, vel[0], vel[1], vel[2],
                        color, sv.time + 10.0, ptype, ramp]]
    sv.time += 0.1
    sv._advance_particles(0.1)
    return sv.particles[0] if sv.particles else None


# grav = 800 * 0.05 * 0.1 = 4.0 on e1m1; dvel = 4 * 0.1 = 0.4

def test_static_has_no_gravity():
    p = _one(_boot(), (100.0, 0.0, 50.0), PT_STATIC)
    assert p[3] == 100.0 and p[5] == 50.0, "static particle must not change velocity"
    assert p[0] == 10.0, "org should still advance by vel*dt"


def test_fire_rises_and_cools():
    sv = _boot()
    p = _one(sv, (0.0, 0.0, 0.0), PT_FIRE, ramp=0.0)
    assert abs(p[5] - 4.0) < 1e-9, "fire rises: vel.z += grav"
    assert abs(p[9] - 0.5) < 1e-9, "ramp += time1 (5*dt)"
    assert p[6] == _RAMP3[0], "fire colour follows ramp3"


def test_fire_dies_when_ramp_maxes():
    p = _one(_boot(), (0.0, 0.0, 0.0), PT_FIRE, ramp=5.8)   # 5.8 + 0.5 >= 6
    assert p[7] == -1.0, "fire particle should be marked dead (die = -1)"


def test_explode_accelerates_and_falls():
    p = _one(_boot(), (100.0, 0.0, 0.0), PT_EXPLODE)
    assert abs(p[3] - 140.0) < 1e-9, "vel += vel*dvel (4x): 100 -> 140"
    assert abs(p[5] - (-4.0)) < 1e-9, "vel.z -= grav"


def test_explode2_decelerates_at_1x_not_4x():
    p = _one(_boot(), (100.0, 0.0, 0.0), PT_EXPLODE2)
    # id: vel -= vel*frametime  => 100 - 100*0.1 = 90  (NOT 100 - 100*0.4 = 60)
    assert abs(p[3] - 90.0) < 1e-9, f"explode2 decel should be 1x (90), got {p[3]}"
    assert abs(p[5] - (-4.0)) < 1e-9, "vel.z -= grav"


def test_blob_accelerates():
    p = _one(_boot(), (100.0, 0.0, 0.0), PT_BLOB)
    assert abs(p[3] - 140.0) < 1e-9


def test_blob2_decelerates_xy_only():
    p = _one(_boot(), (100.0, 100.0, 100.0), PT_BLOB2)
    # xy use dvel (0.4): 100 - 100*0.4 = 60 ; z only gets gravity
    assert abs(p[3] - 60.0) < 1e-9 and abs(p[4] - 60.0) < 1e-9
    assert abs(p[5] - (100.0 - 4.0)) < 1e-9, "blob2 z: gravity only, no decel"


def test_grav_and_slowgrav_fall():
    for pt in (PT_GRAV, PT_SLOWGRAV):
        p = _one(_boot(), (0.0, 0.0, 0.0), pt)
        assert abs(p[5] - (-4.0)) < 1e-9, "grav/slowgrav fall by -grav"


def test_short_particle_is_treated_as_static():
    # the zbuf/overlay renderers inject 8-field particles; advancing must not crash
    sv = _boot()
    sv.particles[:] = [[0.0, 0.0, 0.0, 10.0, 0.0, 20.0, 5, sv.time + 10.0]]
    sv.time += 0.1
    sv._advance_particles(0.1)
    assert sv.particles[0][5] == 20.0, "8-field particle should not get gravity"


if __name__ == "__main__":
    test_static_has_no_gravity()
    test_fire_rises_and_cools()
    test_fire_dies_when_ramp_maxes()
    test_explode_accelerates_and_falls()
    test_explode2_decelerates_at_1x_not_4x()
    test_blob_accelerates()
    test_blob2_decelerates_xy_only()
    test_grav_and_slowgrav_fall()
    test_short_particle_is_treated_as_static()
    print("OK")
