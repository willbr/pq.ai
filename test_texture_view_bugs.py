"""Regression tests for the two texture-view bugs (see ideas.md review):

  1. Button textures don't change when pressed. Quake signals a press by setting
     the entity's `frame` (buttons.qc: `self.frame = 1; // use alternate
     textures`) and R_TextureAnimation (r_surf.c) swaps the surface to its
     `+a..+j` alternate chain. We dropped both halves: brush_models() never
     passed `frame`, and _classify_textures discarded the `+a` chains.

  2. Sky is "double layered". A Quake sky miptex is 256x128 = two stacked
     128x128 layers (left = transparent-keyed clouds, right = background) that
     R_InitSky/R_MakeSky composite into one 128-wide tile. We tiled the whole
     256-wide texture, so both halves appeared side by side.

Driven against the real shareware data on e1m1 (it ships +0basebtn/+1basebtn/
+abasebtn and sky4).
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server
from quake.physics import Physics
from quake.render import Renderer

PAK = "quake-shareware/id1/pak0.pak"


def _renderer():
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    b = Bsp(pak.read("maps/e1m1.bsp"))
    return b, Renderer(b, palette)


def _boot(mapn):
    pak = Pak(PAK)
    b = Bsp(pak.read(f"maps/{mapn}.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname=f"maps/{mapn}.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    for _ in range(3):
        sv.run_frame(0.1)
    return sv


def _miptex(b, name):
    for mt, t in enumerate(b.textures):
        if t and t[0].lower() == name:
            return mt
    raise AssertionError(f"texture {name!r} not in map")


def _face_using(b, r, name):
    """First world/brush face whose base texture is `name`."""
    for fi in range(len(r.face_verts)):
        ti = b.faces[fi][4]
        mt = b.texinfo[ti][0]
        if b.textures[mt] and b.textures[mt][0].lower() == name:
            return fi
    raise AssertionError(f"no face uses {name!r}")


# ---- bug 1: button alternate textures -------------------------------------

def test_alternate_chain_built():
    """+0basebtn must map to an alternate chain that contains +abasebtn."""
    b, r = _renderer()
    base = _miptex(b, "+0basebtn")
    alt = _miptex(b, "+abasebtn")
    assert r.tex_alt[base] is not None, "no alternate chain for +0basebtn"
    assert alt in r.tex_alt[base], (r.tex_alt[base], alt)
    # a texture with no alternate (e.g. a plain +N chain) stays None
    planet = _miptex(b, "+0planet")          # +0..+3planet, no +a
    assert r.tex_alt[planet] is None


def test_brush_models_carry_frame():
    """sv.brush_models() must report each entity's frame so the renderer can
    pick the alternate (pressed) texture."""
    sv = _boot("e1m1")
    vm, ffr = sv.vm, sv.f["frame"]
    ents = sv.brush_models()
    assert ents and all(len(e) == 4 for e in ents), "brush_models lost frame"
    # flip one entity's frame and confirm it propagates
    submodel0, _org, _ang, _fr = ents[0]
    # find the edict backing that first brush model and set frame=1
    fmi = sv.f["modelindex"]
    mp = sv.model_precache
    for num in range(1, vm.num_edicts):
        if vm.free[num]:
            continue
        mi = vm.fget_i(num, fmi)
        if 0 < mi < len(mp) and mp[mi][:1] == "*" and int(mp[mi][1:]) == submodel0:
            vm.fset_f(num, ffr, 1.0)
            break
    again = sv.brush_models()
    assert again[0][3] == 1, "frame change not reflected"


def test_pressed_button_selects_alternate_texture():
    """A brush face whose entity frame is set draws its alternate texture; with
    frame 0 it draws the base. This is the per-face selection the emit loop uses."""
    b, r = _renderer()
    fi = _face_using(b, r, "+0basebtn")
    alt_idx = r.tex_idx[_miptex(b, "+abasebtn")][2]
    rec0 = r.brush_face_tex(fi, 0, 0.0)
    rec1 = r.brush_face_tex(fi, 1, 0.0)
    assert rec1[2] == alt_idx, "pressed button did not switch to +abasebtn"
    assert rec0[2] != alt_idx, "idle button already on the alternate texture"
    # texinfo vectors are preserved across the swap (texture rides the brush)
    assert rec1[3] == rec0[3] and rec1[4] == rec0[4]


# ---- bug 2: sky two-layer composite ---------------------------------------

def test_sky_split_into_two_128_layers():
    """sky4 (256x128) splits into a foreground (left, index 0 = transparent)
    and a background (right), each 128x128."""
    b, r = _renderer()
    mt = _miptex(b, "sky4")
    fg, bg = r.sky_split[mt]
    assert len(fg) == 128 * 128 and len(bg) == 128 * 128
    assert 0 in fg, "foreground layer has no transparent texels"
    # the two layers are the two halves of the source, so they differ
    assert fg != bg


def test_sky_composite_tile_is_single_layer():
    """The per-frame composited sky tile is 128 wide -- one layer, not the
    256-wide double image that produced the 'double layered' look."""
    b, r = _renderer()
    mt = _miptex(b, "sky4")
    tiles = r._make_sky(0.0)
    w, h, tile = tiles[mt]
    assert (w, h) == (128, 128), (w, h)
    assert len(tile) == 128 * 128
    # foreground (non-zero) overlays background: no transparent holes remain
    # unless the background was itself 0 there
    assert tile != r.sky_split[mt][0]   # not just the bare foreground


def test_sky_composite_advances_over_time():
    b, r = _renderer()
    mt = _miptex(b, "sky4")
    t0 = bytes(r._make_sky(0.0)[mt][2])
    t1 = bytes(r._make_sky(3.0)[mt][2])
    assert t0 != t1, "sky did not scroll"


def test_render_runs_with_sky_and_buttons():
    """Smoke test: textured render still produces a full framebuffer."""
    b, r = _renderer()
    org, yaw = b.find_spawn()
    eye = (org[0], org[1], org[2] + 22)
    (fb, w, h), _ = r.render_zbuffer(eye, yaw, 0.0, textured=True, time=1.3)
    assert len(fb) == w * h


if __name__ == "__main__":
    test_alternate_chain_built()
    test_brush_models_carry_frame()
    test_pressed_button_selects_alternate_texture()
    test_sky_split_into_two_128_layers()
    test_sky_composite_tile_is_single_layer()
    test_sky_composite_advances_over_time()
    test_render_runs_with_sky_and_buttons()
    print("OK")
