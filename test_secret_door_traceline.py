"""Regression test: hitscan traceline must clip against SOLID_BSP brush-model
entities (doors, secret doors, func_walls), not just the world and monster/player
bounding boxes.

The bug: shooting a secret door didn't open it. Secret doors are separate
SOLID_BSP submodels; their QC sets th_pain=fd_secret_use so a bullet opens them.
But Server._move_trace only clipped the bullet against the world point hull and
SOLID_SLIDEBOX/SOLID_BBOX bboxes -- it skipped SOLID_BSP entities entirely. So
trace_ent never became the door, T_Damage was never applied, and th_pain never
fired.

This test builds a minimal world (empty hull 0) plus one solid half-space brush
submodel positioned as a SOLID_BSP edict, then fires a ray through it and asserts
the trace reports the door edict as the hit entity.
"""

from physics import Physics
from sv import Server, SOLID_BSP

CONTENTS_EMPTY = -1
CONTENTS_SOLID = -2


class FakeBsp:
    """Just enough of bsp.Bsp for Physics + Server hitscan.

    Model 0 (world): hull 0 is a single empty leaf -> traces nothing.
    Model 1 (brush): hull 0 is one node splitting on plane x=0; x<0 is solid.
    A ray crossing x=0 from the +x side hits the solid at ~the crossing point.
    """
    def __init__(self):
        self.planes = [((1.0, 0.0, 0.0), 0.0, 0)]          # axial X, dist 0
        # node 0: front (x>=0) -> leaf 0 (empty), back (x<0) -> leaf 1 (solid)
        self.nodes = [(0, (-1, -2), 0, 0)]
        self.leafs = [(CONTENTS_EMPTY, 0, 0, 0), (CONTENTS_SOLID, 0, 0, 0)]
        self.clipnodes = []
        self.models = [
            {"headnode": -1, "headnodes": (-1, -1, -1, -1)},   # world: empty hull0
            {"headnode": 0, "headnodes": (0, 0, 0, 0)},        # brush: node 0
        ]


class FakeVM:
    """Duck-typed VM holding a few edicts as field dicts keyed by field name."""
    def __init__(self, ents):
        self.ents = ents                       # list of dict-or-None
        self.num_edicts = len(ents)
        self.free = [e is None for e in ents]

    def fget_f(self, num, slot):
        return float(self.ents[num].get(slot, 0.0))

    def fget_i(self, num, slot):
        return int(self.ents[num].get(slot, 0))

    def fget_v(self, num, slot):
        return self.ents[num].get(slot, (0.0, 0.0, 0.0))


def make_server():
    bsp = FakeBsp()
    phys = Physics(bsp)
    # world (edict 0) + a SOLID_BSP secret door (edict 1) at the origin, model "*1"
    ents = [
        {"modelindex": 1, "solid": 0.0, "origin": (0.0, 0.0, 0.0)},   # world
        {"modelindex": 2, "solid": float(SOLID_BSP), "origin": (0.0, 0.0, 0.0),
         "owner": 0},
    ]
    vm = FakeVM(ents)
    srv = Server.__new__(Server)               # skip the heavy real constructor
    srv.vm = vm
    srv.phys = phys
    srv.bsp = bsp
    srv.model_precache = ["", "maps/test.bsp", "*1"]
    srv.f = {name: name for name in (
        "solid", "absmin", "absmax", "owner", "modelindex", "origin")}
    return srv


def test_bullet_hits_solid_brush_door():
    srv = make_server()
    # fire a ray from +x straight through x=0 into the solid half-space
    frac, endpos, pnorm, allsolid, startsolid, hit_ent = \
        srv._move_trace((10.0, 0.0, 0.0), (-10.0, 0.0, 0.0), 0.0, 0)
    assert hit_ent == 1, f"bullet should hit the SOLID_BSP door (edict 1), got {hit_ent}"
    assert 0.0 < frac < 1.0, f"impact should be partway along the ray, got frac={frac}"
    # impact at x~=0, i.e. ~halfway along the 10->-10 ray
    assert abs(endpos[0]) < 0.5, f"impact x should be ~0, got {endpos[0]}"


def test_bullet_missing_door_hits_nothing():
    srv = make_server()
    # a ray that stays on the +x (empty) side never enters the brush
    frac, endpos, pnorm, allsolid, startsolid, hit_ent = \
        srv._move_trace((10.0, 0.0, 0.0), (10.0, 0.0, 50.0), 0.0, 0)
    assert hit_ent == 0, f"ray missing the door should hit nothing, got {hit_ent}"
    assert frac == 1.0, f"clear ray should have fraction 1.0, got {frac}"


if __name__ == "__main__":
    test_bullet_hits_solid_brush_door()
    test_bullet_missing_door_hits_nothing()
    print("OK")
