"""View-model depth bias (render.py VIEWMODEL_ZSCALE): WinQuake's ziscale hack.

R_AliasDrawModel scales the view model's z-buffer depth by 3 (r_alias.c, ziscale
*= 3 for cl.viewent) so the first-person weapon wins the depth test against the
world it pokes into, and its own coaxial barrel triangles -- whose true depths
are near-equal -- separate enough to stop z-fighting (the shimmer on the
nailgun). The port applies the same 3x bias to the depth only, leaving the
screen projection on the true 1/z.

These tests pin two things against real shareware data on e1m1:
  - the bias is wired through to the rasteriser: the nailgun view model renders
    differently at 3x than at 1x (its self-overlapping triangles re-order);
  - the bias touches ONLY the view model: a frame with no view model is
    byte-identical regardless of the scale (world/monsters unaffected).
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import quake.render as R
from quake.pak import Pak
from quake.bsp import Bsp
from quake.mdl import Mdl
from quake.render import Renderer

PAK = "quake-shareware/id1/pak0.pak"


def _setup():
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    colormap = pak.read("gfx/colormap.lmp")[:64 * 256]
    b = Bsp(pak.read("maps/e1m1.bsp"))
    r = Renderer(b, palette, colormap)
    r.resize(800, 600)
    origin, yaw = b.find_spawn()
    eye = (origin[0], origin[1], origin[2] + 22.0)
    nail = Mdl(pak.read("progs/v_nail.mdl"), palette)
    vm = (nail, nail.frame_verts(0, 0.5), eye, (0.0, yaw, 0.0))
    return r, eye, yaw, vm


def _frame(r, eye, yaw, vm, zscale):
    R.VIEWMODEL_ZSCALE = zscale
    (fb, w, h), _leaf = r.render_zbuffer(eye, yaw, 0.0, view_model=vm,
                                         textured=True, lightstyles=[256] * 64,
                                         time=0.5)
    return bytes(fb)


def test_bias_is_wired_to_the_viewmodel():
    r, eye, yaw, vm = _setup()
    try:
        a = _frame(r, eye, yaw, vm, 1.0)
        b = _frame(r, eye, yaw, vm, 3.0)
    finally:
        R.VIEWMODEL_ZSCALE = 3.0
    diff = sum(1 for x, y in zip(a, b) if x != y)
    assert diff > 0, \
        "VIEWMODEL_ZSCALE had no effect -- the depth bias is not reaching the " \
        "view-model rasteriser"


def test_bias_does_not_touch_the_world():
    r, eye, yaw, _vm = _setup()
    try:
        a = _frame(r, eye, yaw, None, 1.0)     # no view model
        b = _frame(r, eye, yaw, None, 3.0)
    finally:
        R.VIEWMODEL_ZSCALE = 3.0
    assert a == b, \
        "the view-model depth bias changed world rendering -- it must apply " \
        "only to the view model"


def test_render_is_deterministic():
    r, eye, yaw, vm = _setup()
    try:
        a = _frame(r, eye, yaw, vm, 3.0)
        b = _frame(r, eye, yaw, vm, 3.0)
    finally:
        R.VIEWMODEL_ZSCALE = 3.0
    assert a == b, "same inputs rendered two different frames"


if __name__ == "__main__":
    test_bias_is_wired_to_the_viewmodel()
    test_bias_does_not_touch_the_world()
    test_render_is_deterministic()
    print("OK")
