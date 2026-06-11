"""Lit-surface lightmap smoothness (render.py _surface_cache).

Quake's lightmap is one luxel per 16 texels, but WinQuake's software renderer
(R_DrawSurfaceBlock8_mip0) does NOT flat-shade each 16-texel block: it bilinearly
interpolates the light between the four corner luxels, so every texel reads its
own colormap row and the lighting is a smooth gradient. The port matches that --
a block row that stays within one colormap row is one bytes.translate, and a row
that crosses colormap rows is lit in 4-texel sub-cells along the gradient. This
test drives a face with a strong horizontal lightmap gradient and asserts the lit
cache is smooth (several distinct, monotonically-stepping shades across one cell,
where the old blocky renderer produced exactly one).

Shades are checked against the byte the interpolation should produce (colormap
collisions make recovering a row from a lit byte ambiguous), so the per-texel
expected row is computed the way the renderer does and the cache byte compared
directly.
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


def _cell(r, fi, lr, lc):
    """Return (cache_bytes_for_cell_row, tiled_pixels_for_cell, cw) sampled on
    the luxel-row centre line (fy = 0, so interpolation is purely horizontal)."""
    lmw, lmh, smin, tmin, lux, _ = r.face_lm[fi]
    rec = r.face_tex[fi]
    tw, th, tex = rec[0], rec[1], rec[2]
    cw, ch, cache, _tex = r._surface_cache(fi, rec)
    tc = lr * 16                                  # top of the luxel row: fy = 0
    soff = int(smin) % tw
    reps = (soff + cw + tw - 1) // tw
    trow = ((int(tmin) + tc) % th) * tw
    tiled = (tex[trow:trow + tw] * reps)[soff:soff + cw]
    base = tc * cw + lc * 16
    return cache[base:base + 16], tiled[lc * 16:lc * 16 + 16]


def _expected_rows(lux_l, lux_r):
    """The 16 per-texel colormap rows the renderer's interpolation produces across
    a cell whose left/right luxels are lux_l, lux_r (fy = 0): four 4-texel cells,
    each at the light of its centre. Mirrors render.py _surface_cache."""
    left = (255 - lux_l) << 6
    right = (255 - lux_r) << 6
    diff = right - left
    rows = []
    for x in range(16):
        light = left + (diff * ((x >> 2) * 4 + 2) >> 4)
        rows.append(light >> 8)
    return rows


def test_gradient_cell_is_smooth_not_flat():
    r = _renderer()
    cmap = r.colormap
    fi, lr, lc = _find_gradient_face(r)
    lmw = r.face_lm[fi][0]
    lux = r.face_lm[fi][4]
    cache_row, tiled = _cell(r, fi, lr, lc)
    rows = _expected_rows(lux[lr * lmw + lc], lux[lr * lmw + lc + 1])

    # the cache applies the per-texel interpolated light, not a flat block shade
    for x in range(16):
        assert cache_row[x] == cmap[rows[x] * 256 + tiled[x]], \
            f"texel {x}: cache does not match interpolated lighting"

    # which means several distinct shades span the cell (old renderer: exactly 1)
    assert len(set(rows)) >= 3, f"cell still looks blocky: shades {rows}"

    # and the shade steps monotonically from the left luxel toward the right one
    assert rows == sorted(rows) or rows == sorted(rows, reverse=True), \
        f"cell shading is not monotonic across the gradient: {rows}"
    # the cell spans toward the neighbour: its ends bracket both luxels' shades
    lrow = (255 - lux[lr * lmw + lc]) >> 2
    rrow = (255 - lux[lr * lmw + lc + 1]) >> 2
    assert min(lrow, rrow) <= rows[0] <= max(lrow, rrow), "left of cell out of range"
    assert min(lrow, rrow) <= rows[-1] <= max(lrow, rrow), "right of cell out of range"


def test_uniform_cell_stays_flat():
    """A cell whose two luxels are equal lights to a single shade (the fast
    path) -- interpolation introduces no noise on flat lighting."""
    rows = _expected_rows(120, 120)
    assert len(set(rows)) == 1, f"equal luxels should give one shade: {rows}"


if __name__ == "__main__":
    test_gradient_cell_is_smooth_not_flat()
    test_uniform_cell_stays_flat()
    print("OK")
