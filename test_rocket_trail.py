"""Regression test: moving missiles leave a particle trail.

Rockets, grenades, fireballs (lavaball) and gibs left no trail. In Quake the
client lays these down per frame (R_RocketTrail in CL_RelinkEntities), keyed off
the *model's* effect flags (EF_ROCKET / EF_GRENADE / EF_GIB / EF_TRACER*) and the
distance the entity moved since last frame. Nothing read those flags or tracked
per-entity movement, so no trail was ever produced.

_emit_trails now reads each model's flags (via the pak) and lays particles along
the segment a flagged entity moved this frame. EF_ROTATE items (which spin, not
trail) must NOT produce one.

Driven against the real shareware progs on e1m1.
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
    return sv


def _spawn(sv, model_name, origin):
    vm, f = sv.vm, sv.f
    e = vm.alloc_edict()
    vm.fset_i(e, f["modelindex"], sv.model_precache.index(model_name))
    vm.fset_v(e, f["origin"], origin)
    return e


def test_missile_lays_a_trail():
    sv = _boot()
    vm, f = sv.vm, sv.f
    e = _spawn(sv, "progs/missile.mdl", (480.0, -352.0, 100.0))
    sv._emit_trails()                       # first sighting: record, no trail
    assert len(sv.particles) == 0
    vm.fset_v(e, f["origin"], (520.0, -352.0, 100.0))   # moved 40 units
    sv._emit_trails()
    assert len(sv.particles) > 1, "missile left no trail after moving"


def test_lavaball_and_grenade_trail():
    sv = _boot()
    vm, f = sv.vm, sv.f
    for name in ("progs/lavaball.mdl", "progs/grenade.mdl"):
        sv.particles = []
        sv._ent_lastorg = {}
        e = _spawn(sv, name, (480.0, -352.0, 100.0))
        sv._emit_trails()
        vm.fset_v(e, f["origin"], (500.0, -352.0, 100.0))
        sv._emit_trails()
        assert len(sv.particles) > 1, f"{name} left no trail"


def test_spinning_item_has_no_trail():
    """EF_ROTATE pickups (armor, backpack, ...) spin -- they must not trail even
    if they get nudged (droptofloor, a lift carrying them)."""
    sv = _boot()
    vm, f = sv.vm, sv.f
    e = _spawn(sv, "progs/armor.mdl", (480.0, -352.0, 100.0))
    assert sv._trail_type(vm.fget_i(e, f["modelindex"])) is None
    sv._emit_trails()
    vm.fset_v(e, f["origin"], (520.0, -352.0, 100.0))
    sv._emit_trails()
    assert len(sv.particles) == 0, "a spinning item should not leave a trail"


if __name__ == "__main__":
    test_missile_lays_a_trail()
    test_lavaball_and_grenade_trail()
    test_spinning_item_has_no_trail()
    print("OK")
