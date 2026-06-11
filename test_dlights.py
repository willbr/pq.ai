"""Dynamic lights (CL_AllocDlight / R_MarkLights / R_AddDynamicLights).

A dlight pool on the client (keyed per entity, plus explosion events) feeds
the renderer, which walks the BSP marking faces the light sphere touches and
adds the radius falloff into their lightmap luxels for the frame -- restoring
them when the light moves or dies. Sources: EF_MUZZLEFLASH (one-shot, the
server clears the bit after reporting it), EF_BRIGHTLIGHT/EF_DIMLIGHT,
rocket glow, and TE_EXPLOSION (radius 350, 0.5s, decay 300/s).
"""

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer

PAK = "quake-shareware/id1/pak0.pak"
EF_MUZZLEFLASH = 2


def _renderer():
    pak = Pak(PAK)
    pal = pak.read("gfx/palette.lmp")
    palette = [(pal[i*3], pal[i*3+1], pal[i*3+2]) for i in range(256)]
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    return bsp, Renderer(bsp, palette, pak.read("gfx/colormap.lmp")[:64*256])


def _lit_face_and_point(bsp, rend):
    """A face with a real lightmap, and a point 10 units off its centre."""
    for fi in range(len(bsp.faces)):
        rec = rend.face_lm[fi]
        if not rec[5] or rec[0] < 4 or rec[1] < 4:
            continue
        vs = [bsp.vertexes[v] for v in rend.face_verts[fi]]
        cx = sum(v[0] for v in vs) / len(vs)
        cy = sum(v[1] for v in vs) / len(vs)
        cz = sum(v[2] for v in vs) / len(vs)
        n, d, _t = bsp.planes[bsp.faces[fi][0]]
        if bsp.faces[fi][1]:
            n = (-n[0], -n[1], -n[2])
        return fi, (cx + n[0]*10, cy + n[1]*10, cz + n[2]*10)
    raise AssertionError("no lightmapped face?")


def test_dlight_brightens_and_restores_luxels():
    bsp, rend = _renderer()
    fi, pt = _lit_face_and_point(bsp, rend)
    styles = [256] * 64
    before = bytes(rend.face_lm[fi][4])
    rend.apply_dlights([(pt[0], pt[1], pt[2], 200.0, 32.0)], styles)
    after = bytes(rend.face_lm[fi][4])
    assert sum(after) > sum(before), "dlight did not brighten the face"
    assert fi in rend._dlit_faces
    rend.apply_dlights([], styles)              # light gone: restore
    assert bytes(rend.face_lm[fi][4]) == before
    assert not rend._dlit_faces


def test_muzzleflash_makes_a_oneshot_dlight():
    c = client.Client("e1m1")
    c.resize(320, 240)
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    vm.fset_f(e, f["effects"], float(EF_MUZZLEFLASH))
    c.frame(0.016, client.InputState())
    assert c.dlights, "muzzleflash made no dlight"
    assert vm.fget_f(e, f["effects"]) == 0.0, \
        "EF_MUZZLEFLASH not cleared (SV_CleanupEnts)"
    for _ in range(10):                         # die = +0.1s
        c.frame(0.05, client.InputState())
    assert not c.dlights, "muzzleflash never expired"


def test_explosion_event_makes_a_decaying_dlight():
    c = client.Client("e1m1")
    c.resize(320, 240)
    c.sv.dlight_events.append((tuple(c.pos), 350.0, c.sv.time + 0.5, 300.0))
    c.frame(0.016, client.InputState())
    assert c.dlights
    r0 = next(iter(c.dlights.values()))[3]
    c.frame(0.1, client.InputState())
    r1 = next(iter(c.dlights.values()))[3]
    assert r1 < r0, "explosion light did not decay"
    for _ in range(12):
        c.frame(0.05, client.InputState())
    assert not c.dlights, "explosion light never died"


if __name__ == "__main__":
    test_dlight_brightens_and_restores_luxels()
    test_muzzleflash_makes_a_oneshot_dlight()
    test_explosion_event_makes_a_decaying_dlight()
    print("OK")
