"""Lit-surface cache lightmap interpolation (render.py _surface_cache).

Quake's lightmap is one luxel per 16 texels, but WinQuake's R_DrawSurfaceBlock
bilinearly interpolates it across each block, so the lighting is smooth rather
than 16-unit blocks. The port used to light each 16-texel luxel cell with one
flat shade (one bytes.translate per cell), which read as blocky squares. The
cache now blends the luxels across each cell. This test recovers the per-texel
shade from a face with a strong horizontal lightmap gradient and asserts it ramps
across the cell instead of stepping flat.
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


def test_cell_shade_ramps_across_the_gradient():
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

    s0 = lc * 16                       # cell left edge: ~ luxel[lc]
    s1 = lc * 16 + 14                  # near the cell right edge: ~ luxel[lc+1]
    base = lr * 16 + tc - lr * 16      # row index already tc
    left = _recover_shade(r, cache[tc * cw + s0], tiled[s0])
    right = _recover_shade(r, cache[tc * cw + s1], tiled[s1])
    assert left is not None and right is not None, "could not recover shade"
    # a flat cell would give the SAME shade left and right; interpolation ramps it
    assert left != right, \
        f"lightmap cell is flat (no interpolation): shade {left} == {right}"
    # and the ramp goes toward the brighter neighbour (lower colormap row = brighter)
    bright_lc = lux[lr * lmw + lc] > lux[lr * lmw + lc + 1]
    assert (left < right) == bright_lc, \
        "interpolation ramps the wrong way relative to the luxel gradient"


if __name__ == "__main__":
    test_cell_shade_ramps_across_the_gradient()
    print("OK")
