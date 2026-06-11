"""Particle spawn fidelity against id's r_part.c effect functions.

Each spawn path mirrors one r_part.c function: R_RunParticleEffect (spikes,
gunshots), R_ParticleExplosion (alternating pt_explode/pt_explode2),
R_BlobExplosion (pt_blob/pt_blob2), R_LavaSplash / R_TeleportSplash (slowgrav
columns), and R_RocketTrail's six cases. RNG is seeded for determinism.
"""

import random

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import (Server, PT_STATIC, PT_FIRE, PT_GRAV, PT_SLOWGRAV,
                      PT_EXPLODE, PT_EXPLODE2, PT_BLOB, PT_BLOB2, _RAMP1)
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b, mapname="maps/e1m1.bsp",
                skill=1, physics=Physics(b), pak=pak)
    sv.load_level()
    sv.particles[:] = []
    return sv


def test_run_particle_effect_is_slowgrav_dir15():
    # R_RunParticleEffect (non-explosion): vel = dir*15, pt_slowgrav, colour
    # (color & ~7) + rand&7, org jitter +/-8.
    sv = _boot()
    random.seed(1)
    sv._run_particle_effect((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), 0, 20)
    assert len(sv.particles) == 20
    for p in sv.particles:
        assert p[8] == PT_SLOWGRAV
        assert (p[3], p[4], p[5]) == (15.0, 0.0, 0.0), "vel must be dir*15"
        assert 0 <= p[6] <= 7, "gunshot colour (0 & ~7) + rand&7"
        assert -8 <= p[0] <= 7, "org jitter (rand&15)-8"


def test_explosion_alternates_explode_and_explode2():
    sv = _boot()
    random.seed(2)
    sv._particle_explosion((0.0, 0.0, 0.0))
    assert sv.particles, "explosion spawned nothing"
    types = {p[8] for p in sv.particles}
    assert types == {PT_EXPLODE, PT_EXPLODE2}, types
    for p in sv.particles:
        assert p[6] == _RAMP1[0], "explosion seeds colour ramp1[0] = 0x6f"
        assert 0.0 <= p[9] <= 3.0, "ramp seeded rand&3"
        assert -256 <= p[3] <= 255, "vel seeded (rand%512)-256"


def test_blob_explosion_types_and_colors():
    sv = _boot()
    random.seed(3)
    sv._blob_explosion((0.0, 0.0, 0.0))
    assert sv.particles
    for p in sv.particles:
        if p[8] == PT_BLOB:
            assert 66 <= p[6] <= 71, "blob colour 66 + rand%6"
        else:
            assert p[8] == PT_BLOB2
            assert 150 <= p[6] <= 155, "blob2 colour 150 + rand%6"


def test_lava_splash_is_slowgrav_shooting_up():
    sv = _boot()
    random.seed(4)
    sv._lava_splash((0.0, 0.0, 0.0))
    assert sv.particles
    for p in sv.particles:
        assert p[8] == PT_SLOWGRAV
        assert p[5] > 0.0, "lava column shoots upward (dir.z = 256)"
        assert 224 <= p[6] <= 231, "lava colour 224 + rand&7"


def test_teleport_splash_is_slowgrav():
    sv = _boot()
    random.seed(5)
    sv._teleport_splash((0.0, 0.0, 0.0))
    assert sv.particles
    for p in sv.particles:
        assert p[8] == PT_SLOWGRAV
        assert 7 <= p[6] <= 14, "teleport colour 7 + rand&7"


def test_rocket_trail_is_fire():
    # type 0 (EF_ROCKET): pt_fire smoke, zero initial velocity, dense along path.
    sv = _boot()
    random.seed(6)
    sv._rocket_trail((0.0, 0.0, 0.0), (90.0, 0.0, 0.0), 0)
    assert sv.particles
    for p in sv.particles:
        assert p[8] == PT_FIRE
        assert (p[3], p[4], p[5]) == (0.0, 0.0, 0.0), "smoke starts at rest"


def test_blood_trail_is_grav():
    sv = _boot()
    random.seed(7)
    sv._rocket_trail((0.0, 0.0, 0.0), (90.0, 0.0, 0.0), 2)   # gib blood
    assert sv.particles
    for p in sv.particles:
        assert p[8] == PT_GRAV
        assert 67 <= p[6] <= 70, "blood colour 67 + rand&3"


def test_tracer_trail_gets_sideways_velocity():
    # type 3 (EF_TRACER): pt_static, perpendicular 30 u/s, alternating side.
    sv = _boot()
    random.seed(8)
    sv._tracercount = 0
    sv._rocket_trail((0.0, 0.0, 0.0), (30.0, 0.0, 0.0), 3)
    assert sv.particles
    for p in sv.particles:
        assert p[8] == PT_STATIC, "tracers are static (no gravity)"
        assert p[3] == 0.0 and p[5] == 0.0
        assert abs(p[4]) == 30.0, "perpendicular 30 u/s (vec=(1,0,0))"
    sides = {p[4] for p in sv.particles}
    assert sides == {30.0, -30.0}, "alternating tracercount flips the side"


if __name__ == "__main__":
    test_run_particle_effect_is_slowgrav_dir15()
    test_explosion_alternates_explode_and_explode2()
    test_blob_explosion_types_and_colors()
    test_lava_splash_is_slowgrav_shooting_up()
    test_teleport_splash_is_slowgrav()
    test_rocket_trail_is_fire()
    test_blood_trail_is_grav()
    test_tracer_trail_gets_sideways_velocity()
    print("OK")
