"""Regression test: external .bsp pickup models (health/ammo boxes) render.

Bug: health and ammo pickups were invisible. In Quake these use standalone
maps/b_*.bsp brush models -- not inline '*N' world submodels and not .mdl alias
models -- so they fell through both the brush-model path (filters '*N') and the
alias path (filters '.mdl'), and were never loaded or drawn.

Covers three seams of the fix:
  1. Server.bsp_model_entities() enumerates external-.bsp entities (and excludes
     '*N' submodels, .mdl models, and the world map at index 1).
  2. PickupModel loads a real shareware b_*.bsp into flat-shaded faces.
  3. Renderer.render_shaded actually emits polygons for a pickup placed in front
     of the camera.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer, PickupModel
from quake.sv import Server, SOLID_BSP

PAK = "quake-shareware/id1/pak0.pak"


# --- 1. enumeration --------------------------------------------------------
class FakeVM:
    def __init__(self, ents):
        self.ents = ents
        self.num_edicts = len(ents)
        self.free = [e is None for e in ents]

    def fget_i(self, num, slot):
        return int(self.ents[num].get(slot, 0))

    def fget_f(self, num, slot):
        return float(self.ents[num].get(slot, 0.0))

    def fget_v(self, num, slot):
        return self.ents[num].get(slot, (0.0, 0.0, 0.0))


def test_enumeration_picks_external_bsp_only():
    # model_precache: 0 empty, 1 world, 2 a pickup .bsp, 3 a door submodel,
    # 4 a monster .mdl
    precache = ["", "maps/e1m1.bsp", "maps/b_bh25.bsp", "*3", "progs/army.mdl"]
    # `model` is a nonzero string offset for a live entity; 0 means string_null
    # (a picked-up item the engine no longer renders).
    ents = [
        {},                                              # 0 world
        {"modelindex": 2, "model": 9, "origin": (1.0, 2.0, 3.0), "angles": (0.0, 90.0, 0.0)},
        {"modelindex": 3, "model": 9},                   # a door (inline submodel)
        {"modelindex": 4, "model": 9},                   # a monster (.mdl)
        {"modelindex": 1, "model": 9},                   # something on the world map
        {"modelindex": 2, "model": 0, "origin": (5.0, 6.0, 7.0)},  # picked-up box
    ]
    srv = Server.__new__(Server)
    srv.vm = FakeVM(ents)
    srv.model_precache = precache
    srv.f = {n: n for n in ("modelindex", "model", "origin", "angles")}
    got = srv.bsp_model_entities()
    assert got == [(2, (1.0, 2.0, 3.0), (0.0, 90.0, 0.0))], got


# --- 2. model loading ------------------------------------------------------
def _palette(pak):
    pal = pak.read("gfx/palette.lmp")
    return [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]


def test_pickup_model_loads_real_bsp():
    pak = Pak(PAK)
    pm = PickupModel(Bsp(pak.read("maps/b_bh25.bsp")), _palette(pak))
    assert pm.faces, "pickup model should have faces"
    assert pm.maxs[0] > pm.mins[0], f"degenerate bounds {pm.mins}..{pm.maxs}"
    textured = 0
    for verts, plane, color, rgb, texrec, svec, tvec in pm.faces:
        assert len(verts) >= 3
        assert color.startswith("#") and len(color) == 7
        assert len(rgb) == 3 and all(0 <= c <= 255 for c in rgb)
        if texrec is not None:
            w, h, pix = texrec
            assert w > 0 and h > 0 and len(pix) == w * h   # palette indices
            assert svec is not None and tvec is not None
            textured += 1
    assert textured, "pickup faces should carry decoded textures for textured mode"


# --- 3. it actually renders -----------------------------------------------
def test_pickup_renders_in_front_of_camera():
    pak = Pak(PAK)
    palette = _palette(pak)
    world = Bsp(pak.read("maps/e1m1.bsp"))
    rend = Renderer(world, palette)
    pm = PickupModel(Bsp(pak.read("maps/b_bh25.bsp")), palette)

    (sx, sy, sz), yaw = world.find_spawn()
    eye = (sx, sy, sz + 22.0)
    # drop a health box ahead, nudged off-axis and below eye level so several of
    # its faces are oblique to the camera (not a single edge-on wall)
    import math
    yr = math.radians(yaw)
    box = (sx + 56.0 * math.cos(yr) - 20.0, sy + 56.0 * math.sin(yr) + 20.0,
           sz - 8.0)

    base, _ = rend.render_shaded(eye, yaw, 0.0)
    withbox, _ = rend.render_shaded(eye, yaw, 0.0,
                                    bsp_ents=[(pm, box, (0.0, 0.0, 0.0))])
    assert len(withbox) > len(base), (
        f"pickup added no polygons (base={len(base)}, with={len(withbox)}) -- "
        "the box should be visible in front of the spawn")


def test_pickup_textured_in_zbuffer():
    """In textured z-buffer mode the box must be drawn with its decoded texture,
    not a flat colour -- so the textured framebuffer differs from the flat one."""
    pak = Pak(PAK)
    palette = _palette(pak)
    world = Bsp(pak.read("maps/e1m1.bsp"))
    rend = Renderer(world, palette)
    pm = PickupModel(Bsp(pak.read("maps/b_bh25.bsp")), palette)

    (sx, sy, sz), yaw = world.find_spawn()
    eye = (sx, sy, sz + 22.0)
    import math
    yr = math.radians(yaw)
    box = (sx + 56.0 * math.cos(yr) - 20.0, sy + 56.0 * math.sin(yr) + 20.0,
           sz - 8.0)
    ents = [(pm, box, (0.0, 0.0, 0.0))]

    (fb_none, w, h), _ = rend.render_zbuffer(eye, yaw, 0.0, textured=True)
    (fb_tex, _, _), _ = rend.render_zbuffer(eye, yaw, 0.0, bsp_ents=ents,
                                            textured=True)
    (fb_flat, _, _), _ = rend.render_zbuffer(eye, yaw, 0.0, bsp_ents=ents,
                                             textured=False)
    drawn = sum(1 for i in range(len(fb_tex)) if fb_tex[i] != fb_none[i])
    assert drawn > 0, "pickup drew nothing in textured z-buffer mode"
    # textured pixels must differ from the flat-colour fill of the same faces
    diff = sum(1 for i in range(len(fb_tex)) if fb_tex[i] != fb_flat[i])
    assert diff > 0, "textured pickup looks identical to flat -- not texture-mapped"


if __name__ == "__main__":
    test_enumeration_picks_external_bsp_only()
    test_pickup_model_loads_real_bsp()
    test_pickup_renders_in_front_of_camera()
    test_pickup_textured_in_zbuffer()
    print("OK")
