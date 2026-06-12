"""Lightning beams (TE_LIGHTNING1/2/3, cl_tent.c CL_ParseBeam/CL_UpdateTEnts).

W_FireLightning broadcasts WriteByte(SVC_TEMPENTITY) WriteByte(TE_LIGHTNING2)
WriteEntity(self) then six WriteCoords (start, end). The server decodes that
into a live beam (0.2 s, one per owner entity, re-fired each frame while the
trigger is held); the client chops it into 30-unit bolt-model segments that
ride the existing alias-model render path -- so the lightning gun is finally
visible.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from quake.sv import IT_LIGHTNING, IT_CELLS


def _boot_firing():
    c = client.Client("e1m1")
    c.resize(320, 240)
    sv, f, vm, e = c.sv, c.sv.f, c.sv.vm, c.sv.player
    items = int(vm.fget_f(e, f["items"]))
    vm.fset_f(e, f["items"], float(items | IT_LIGHTNING | IT_CELLS))
    vm.fset_f(e, f["weapon"], float(IT_LIGHTNING))
    vm.fset_f(e, f["ammo_cells"], 50.0)
    vm.fset_f(e, f["currentammo"], 50.0)
    sv._exec_named("W_SetCurrentAmmo", e)
    return c


def test_lightning_gun_produces_a_live_beam():
    c = _boot_firing()
    inp = client.InputState(fire=True)
    for _ in range(4):
        c.frame(0.05, inp)
    beams = c.sv.live_beams()
    assert beams, "firing the lightning gun made no beam"
    b = beams[0]
    assert b["ent"] == c.sv.player
    assert b["model"] == "progs/bolt2.mdl"
    span = sum((b["end"][i] - b["start"][i]) ** 2 for i in range(3)) ** 0.5
    assert span > 30.0, f"beam too short to be a lightning trace ({span:.0f})"
    # one beam per owner: continuous fire re-feeds it, never stacks
    assert len([x for x in beams if x["ent"] == c.sv.player]) == 1


def test_beam_expires_when_fire_released():
    c = _boot_firing()
    c.frame(0.05, client.InputState(fire=True))
    assert c.sv.live_beams()
    for _ in range(8):                          # 0.4 s without re-firing
        c.frame(0.05, client.InputState())
    assert not c.sv.live_beams(), "beam outlived its 0.2s"


def test_client_renders_bolt_segments():
    c = _boot_firing()
    inp = client.InputState(fire=True)
    for _ in range(3):
        c.frame(0.05, inp)
    segs = c._beam_ents()
    beams = c.sv.live_beams()
    span = sum((beams[0]["end"][i] - beams[0]["start"][i]) ** 2
               for i in range(3)) ** 0.5
    import math
    assert len(segs) == math.ceil(span / 30), \
        f"expected one bolt model per 30 units ({span:.0f}u, {len(segs)} segs)"
    mdl, verts, org, ang = segs[0]
    assert verts, "segment has no geometry"


if __name__ == "__main__":
    test_lightning_gun_produces_a_live_beam()
    test_beam_expires_when_fire_released()
    test_client_renders_bolt_segments()
    print("OK")
