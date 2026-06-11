"""Lit-surface cache lightmap blockiness (render.py _surface_cache).

Quake's lightmap is one luxel per 16 texels. WinQuake's D_CacheSurface lights each
16-texel luxel cell with one flat shade (R_DrawSurfaceBlock), so the cache reads as
16-unit blocks -- the span renderer's fast path is one fetch per pixel with no
lightmap math. The span/edge port matches this: each cell is one bytes.translate
through its luxel's colormap row. This test recovers the per-texel shade from a
face with a strong horizontal lightmap gradient and asserts each cell is flat and
that adjacent cells step in the direction of the luxel gradient.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer

PAK = "quake-shareware/id1/pak0.pak"


def _renderer():
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    colormap = pak.read("gfx/colormap.lmp")[:64 * 256]
    b = Bsp(pak.read("maps/e1m1.bsp"))
    r = Renderer(b, palette, colormap)
    r.resize(800, 600)
    return r


def _find_gradient_face(r):
    """A face with two horizontally adjacent luxels that differ a lot."""
    for fi in range(len(r.face_lm)):
        lmw, lmh, smin, tmin, lux, has = r.face_lm[fi]
        if not has or lmw < 2 or lmh < 2:
            continue
        for lr in range(lmh):
            for lc in range(lmw - 1):
                if abs(lux[lr * lmw + lc] - lux[lr * lmw + lc + 1]) > 50:
                    return fi, lr, lc
    raise AssertionError("no face with a strong lightmap gradient on e1m1")


def _recover_shade(r, lit_byte, base_idx):
    """The colormap row (0=bright..63=dark) that maps base_idx -> lit_byte."""
    cmap = r.colormap
    for row in range(64):
        if cmap[row * 256 + base_idx] == lit_byte:
            return row
    return None


def test_cell_is_flat_and_cells_step_with_the_gradient():
    r = _renderer()
    fi, lr, lc = _find_gradient_face(r)
    lmw, lmh, smin, tmin, lux, _ = r.face_lm[fi]
    rec = r.face_tex[fi]
    tw, th, tex = rec[0], rec[1], rec[2]
    cw, ch, cache, _tex = r._surface_cache(fi, rec)

    # recompute the tiled texture row for a texel row inside this luxel cell,
    # so we know the base index under each cache byte (cache = colormap[row][idx])
    tc = lr * 16 + 8
    soff = int(smin) % tw
    reps = (soff + cw + tw - 1) // tw
    trow = ((int(tmin) + tc) % th) * tw
    tiled = (tex[trow:trow + tw] * reps)[soff:soff + cw]

    # within cell lc, every texel uses the SAME colormap row -- blocky, no blend
    s_lo = lc * 16 + 1
    s_hi = lc * 16 + 14
    left = _recover_shade(r, cache[tc * cw + s_lo], tiled[s_lo])
    right = _recover_shade(r, cache[tc * cw + s_hi], tiled[s_hi])
    assert left is not None and right is not None, "could not recover shade"
    assert left == right, \
        f"cell is not flat (blocky cache expected): shade {left} != {right}"

    # the next cell (lc+1) steps to a different shade, in the luxel-gradient
    # direction (brighter luxel -> lower colormap row)
    s_next = (lc + 1) * 16 + 8
    nxt = _recover_shade(r, cache[tc * cw + s_next], tiled[s_next])
    assert nxt is not None and nxt != left, \
        "adjacent cells should differ across a strong luxel gradient"
    lc_brighter = lux[lr * lmw + lc] > lux[lr * lmw + lc + 1]
    assert (left < nxt) == lc_brighter, \
        "cell shades step the wrong way relative to the luxel gradient"


if __name__ == "__main__":
    test_cell_is_flat_and_cells_step_with_the_gradient()
    print("OK")
