""".spr sprite models (spritegn.h / r_sprite.c R_DrawSprite).

QC's BecomeExplosion sets progs/s_explod.spr as the entity model and cycles
frames 0-5, and drowning spawns s_bubble.spr entities -- so without sprite
support every explosion is invisible. Spr parses the IDSP header and frames
(palette-indexed, 255 transparent); the zbuf renderer billboards them facing
the camera with a per-pixel depth test.
"""

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from quake.pak import Pak
from quake.spr import Spr

PAK = "quake-shareware/id1/pak0.pak"


def test_spr_parses_explosion_sprite():
    spr = Spr(Pak(PAK).read("progs/s_explod.spr"))
    assert spr.numframes == 6
    for i in range(6):
        offx, offy, w, h, pix = spr.frame(i)
        assert 0 < w <= 56 and 0 < h <= 56
        assert len(pix) == w * h
        assert offx <= 0 <= offy            # origin offsets centre the frame


def test_renderer_billboards_a_sprite():
    c = client.Client("e1m1")
    c.resize(320, 240)
    spr = Spr(c.pak.read("progs/s_explod.spr"))
    fwd_eye = (c.pos[0], c.pos[1], c.pos[2] + 22.0)
    import math
    yawr = math.radians(c.yaw)
    spot = (fwd_eye[0] + math.cos(yawr) * 80.0,
            fwd_eye[1] + math.sin(yawr) * 80.0, fwd_eye[2])
    without, _ = c.rend.render_zbuffer(fwd_eye, c.yaw, 0.0)
    withspr, _ = c.rend.render_zbuffer(fwd_eye, c.yaw, 0.0,
                                       sprites=[(spr.frame(2), spot)])
    assert withspr[0] != without[0], "sprite drew nothing"


def test_sprite_entities_reach_the_client():
    c = client.Client("e1m1")
    c.resize(320, 240)
    sv, f, vm = c.sv, c.sv.f, c.sv.vm
    assert "progs/s_explod.spr" in sv.model_precache  # W_Precache did this
    mi = sv.model_precache.index("progs/s_explod.spr")
    e = vm.alloc_edict()
    vm.fset_i(e, f["model"], sv.pr.new_string("progs/s_explod.spr"))
    vm.fset_i(e, f["modelindex"], mi)
    vm.fset_v(e, f["origin"], (c.pos[0], c.pos[1], c.pos[2] + 40.0))
    vm.fset_f(e, f["frame"], 2.0)

    ents = sv.sprite_entities()
    assert (mi, (c.pos[0], c.pos[1], c.pos[2] + 40.0), 2) in ents
    resolved = c._sprite_ents()
    assert resolved and resolved[0][0][2] > 0   # (frame tuple w, ...) present
    c.mode = "zbuf"
    c.frame(0.05, client.InputState())          # renders without error


if __name__ == "__main__":
    test_spr_parses_explosion_sprite()
    test_renderer_billboards_a_sprite()
    test_sprite_entities_reach_the_client()
    print("OK")
