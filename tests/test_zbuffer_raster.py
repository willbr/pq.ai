"""Tests for the z-buffer rasteriser's span geometry + golden-frame pinning.

Two layers:

  1. Unit tests for the pure span helpers: poly_spans (convex polygon ->
     per-scanline x-intervals at pixel centres) and plane_gradients (screen-
     space d/dx, d/dy of attributes linear in screen space: 1/z, u/z, v/z).

  2. Golden-frame characterisation of render_zbuffer against real e1m1 data.
     Goldens live in quake-shareware/goldens/ (gitignored, derived from id
     textures); regenerate with `python test_zbuffer_raster.py --regen`.
     Comparison is tolerant (a small share of boundary pixels may differ when
     the fill rule changes) so the rasteriser can be reworked for speed while
     pinning the image: same geometry, same textures, same lighting.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os
import sys

from quake.pak import Pak
from quake.bsp import Bsp
from quake.mdl import Mdl
import quake.render as render_mod
from quake.render import (Renderer, PickupModel, poly_spans, plane_gradients,
                          screen_backface)

PAK = "quake-shareware/id1/pak0.pak"
GOLDEN_DIR = "quake-shareware/goldens"

# golden tolerances: boundary pixels may move when the fill rule changes, but
# the image must stay the same -- few differing pixels, tiny mean error.
MAX_DIFF_FRAC = 0.03           # <= 3% of pixels may differ at all
MAX_MEAN_ABS = 1.5             # mean |channel delta| over the whole frame


# ---- unit tests: span geometry ----

def test_poly_spans_square():
    # 10x10 square with corners on integer pixel boundaries: pixels 0..9 in
    # both axes have their centres inside.
    sx = [0.0, 10.0, 10.0, 0.0]
    sy = [0.0, 0.0, 10.0, 10.0]
    y0, spans = poly_spans(sx, sy, 100, 100)
    assert y0 == 0, y0
    assert len(spans) == 10, len(spans)
    for xl, xr in spans:
        assert (xl, xr) == (0, 10), (xl, xr)


def test_poly_spans_half_offset():
    # shifted by +0.5: centres 0.5..9.5 are in [0.5, 10.5) -> still rows 0..9,
    # pixels 0..9 (centre 10.5 is exactly at the right edge, excluded).
    sx = [0.5, 10.5, 10.5, 0.5]
    sy = [0.5, 0.5, 10.5, 10.5]
    y0, spans = poly_spans(sx, sy, 100, 100)
    assert y0 == 0, y0
    assert len(spans) == 10, len(spans)
    for xl, xr in spans:
        assert (xl, xr) == (0, 10), (xl, xr)


def test_poly_spans_triangle_matches_edge_functions():
    # brute-force oracle: a pixel centre is inside iff all three edge
    # functions agree in sign (the old rasteriser's test, boundary excluded).
    tri = ([3.2, 17.8, 6.1], [2.7, 9.4, 18.3])
    w = h = 24
    y0, spans = poly_spans(tri[0], tri[1], w, h)
    ax, ay, bx, by, cx, cy = tri[0][0], tri[1][0], tri[0][1], tri[1][1], tri[0][2], tri[1][2]
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    covered = set()
    for r, (xl, xr) in enumerate(spans):
        for x in range(xl, xr):
            covered.add((x, y0 + r))
    for y in range(h):
        for x in range(w):
            px, py = x + 0.5, y + 0.5
            w0 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
            w1 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
            w2 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            if area < 0:
                w0, w1, w2 = -w0, -w1, -w2
            strict_in = w0 > 1e-9 and w1 > 1e-9 and w2 > 1e-9
            strict_out = w0 < -1e-9 or w1 < -1e-9 or w2 < -1e-9
            if strict_in:
                assert (x, y) in covered, (x, y)
            elif strict_out:
                assert (x, y) not in covered, (x, y)
            # exactly-on-edge centres may go either way


def test_poly_spans_clamps_to_framebuffer():
    # polygon hanging off every side of a 8x6 buffer
    sx = [-5.0, 12.0, 12.0, -5.0]
    sy = [-3.0, -3.0, 9.0, 9.0]
    y0, spans = poly_spans(sx, sy, 8, 6)
    assert y0 == 0, y0
    assert len(spans) == 6, len(spans)
    for xl, xr in spans:
        assert (xl, xr) == (0, 8), (xl, xr)


def test_poly_spans_empty_cases():
    # fully above the buffer
    y0, spans = poly_spans([0.0, 5.0, 2.0], [-9.0, -9.0, -2.0], 10, 10)
    assert spans == [], spans
    # degenerate: zero-height sliver between two pixel-centre rows
    y0, spans = poly_spans([0.0, 5.0, 5.0], [3.1, 3.1, 3.4], 10, 10)
    assert spans == [] or all(xl >= xr for xl, xr in spans), spans


def test_poly_spans_no_cracks_or_overlap():
    # two triangles sharing the diagonal of a quad must tile it exactly:
    # every interior pixel covered once, none twice.
    quad = ([2.3, 19.7, 18.1, 3.9], [1.8, 3.2, 14.6, 13.1])
    t1 = ([quad[0][0], quad[0][1], quad[0][2]], [quad[1][0], quad[1][1], quad[1][2]])
    t2 = ([quad[0][0], quad[0][2], quad[0][3]], [quad[1][0], quad[1][2], quad[1][3]])
    counts = {}
    for sx, sy in (t1, t2):
        y0, spans = poly_spans(sx, sy, 32, 32)
        for r, (xl, xr) in enumerate(spans):
            for x in range(xl, xr):
                counts[(x, y0 + r)] = counts.get((x, y0 + r), 0) + 1
    assert counts, "no pixels covered at all"
    assert all(c == 1 for c in counts.values()), \
        sorted(k for k, c in counts.items() if c > 1)[:10]
    # and the union must equal the quad's own spans
    y0, spans = poly_spans(quad[0], quad[1], 32, 32)
    whole = set()
    for r, (xl, xr) in enumerate(spans):
        for x in range(xl, xr):
            whole.add((x, y0 + r))
    assert whole == set(counts), (len(whole), len(counts))


def test_plane_gradients_recovers_linear_attr():
    # attr = 3 + 2x - y sampled at the corners of a convex quad must come back
    # as exactly that plane.
    sx = [1.0, 9.0, 11.0, 2.0]
    sy = [1.0, 2.0, 8.0, 9.0]
    attr = [3.0 + 2.0 * x - y for x, y in zip(sx, sy)]
    grads = plane_gradients(sx, sy, [attr])
    assert grads is not None
    a0, adx, ady = grads[0]
    assert abs(a0 - 3.0) < 1e-9 and abs(adx - 2.0) < 1e-9 and abs(ady + 1.0) < 1e-9, \
        (a0, adx, ady)
    # several attrs in one call
    attr2 = [-1.0 + 0.25 * x + 4.0 * y for x, y in zip(sx, sy)]
    g2 = plane_gradients(sx, sy, [attr, attr2])
    assert len(g2) == 2
    b0, bdx, bdy = g2[1]
    assert abs(b0 + 1.0) < 1e-9 and abs(bdx - 0.25) < 1e-9 and abs(bdy - 4.0) < 1e-9


def test_plane_gradients_degenerate_returns_none():
    # collinear points span no plane
    sx = [0.0, 1.0, 2.0, 3.0]
    sy = [0.0, 1.0, 2.0, 3.0]
    assert plane_gradients(sx, sy, [[0.0, 1.0, 2.0, 3.0]]) is None


# ---- surface cache ----

def _real_lm_face(r):
    """First world face with a real lightmap and a texture."""
    for fi in range(len(r.face_lm)):
        if r.face_lm[fi][5] and r.face_tex[fi] is not None:
            return fi
    raise AssertionError("no lightmapped textured face on e1m1?")


def test_surface_cache_matches_direct_math():
    # cache texel (sc, tc) is the texture's palette index (wrapped at smin+sc,
    # tmin+tc) mapped through the BILINEARLY interpolated colormap row for that
    # texel (render.py _surface_cache, mirroring R_DrawSurfaceBlock8_mip0): the
    # light is interpolated between the four corner luxels of the 16-texel block;
    # a block row that stays in one colormap row is flat, otherwise the row comes
    # from the light at the texel's 4-texel sub-cell centre. row (255-lux)>>2,
    # row 0 brightest (id's R_BuildLightMap inversion).
    _b, r = _renderer()
    fi = _real_lm_face(r)
    rec = r.face_tex[fi]
    tw, th, tex = rec[0], rec[1], rec[2]          # tex = palette index bytes
    lmw, lmh, smin, tmin, lux, _ = r.face_lm[fi]
    cw, ch, cache, _tex = r._surface_cache(fi, rec)
    assert cw == lmw * 16 and ch == lmh * 16, (cw, ch, lmw, lmh)
    cmap = r.colormap
    smin_i, tmin_i = int(smin), int(tmin)

    def expect_row(sc, tc):
        lc = sc >> 4; lc1 = lc + 1 if lc + 1 < lmw else lc
        br = tc >> 4; br1 = br + 1 if br + 1 < lmh else br
        fy = tc & 15
        lf = lambda lr, cc: (255 - lux[lr * lmw + cc]) << 6
        a, c = lf(br, lc), lf(br1, lc)
        b, d = lf(br, lc1), lf(br1, lc1)
        left = a + ((c - a) * fy >> 4)
        right = b + ((d - b) * fy >> 4)
        if left >> 8 == right >> 8:
            return left >> 8
        sub = (sc & 15) >> 2                       # 4-texel sub-cell, centre light
        return (left + ((right - left) * (sub * 4 + 2) >> 4)) >> 8

    for sc, tc in ((0, 0), (1, 0), (15, 0), (16, 0), (cw - 1, ch - 1),
                   (cw // 2, ch // 2), (3, ch - 1)):
        ti = tex[((tmin_i + tc) % th) * tw + ((smin_i + sc) % tw)]
        assert cache[tc * cw + sc] == cmap[expect_row(sc, tc) * 256 + ti], (sc, tc)


def test_surface_cache_reuse_and_invalidation():
    _b, r = _renderer()
    fi = _real_lm_face(r)
    rec = r.face_tex[fi]
    c1 = r._surface_cache(fi, rec)
    assert r._surface_cache(fi, rec) is c1, "same inputs must hit the cache"
    # a recombine at unchanged brightnesses keeps the same lit variant (the
    # luxels are a pure function of the style values, so nothing is stale)
    r._combine_face(fi, [256] * 64)
    assert r._surface_cache(fi, rec) is c1, "same brightness must reuse"
    # a brightness change switches to a different variant...
    r._combine_face(fi, [128] * 64)
    c2 = r._surface_cache(fi, rec)
    assert c2 is not c1, "changed brightness must use another variant"
    # ...and flicking back reuses the first variant instead of rebuilding
    # (this is what kills the per-style-tick rebuild spike on flickering lights)
    r._combine_face(fi, [256] * 64)
    assert r._surface_cache(fi, rec) is c1, "old brightness must hit its variant"
    # a texture swap (+N animation) must rebuild too (bytes(bytes) would be
    # the same object in CPython, so go through bytearray to get a new one)
    swapped = (rec[0], rec[1], bytes(bytearray(rec[2])), rec[3], rec[4])
    c3 = r._surface_cache(fi, swapped)
    assert c3 is not c1, "texture swap must invalidate"


# ---- golden-frame characterisation ----

# ---- alias-model backface culling (the v_nail.mdl z-fight) ----

def test_screen_backface_winding():
    # Screen coords are y-down. WinQuake (d_polyse.c) culls a triangle when the
    # signed area is >= 0 (clockwise). A CCW-in-y-down triangle is kept.
    front = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))   # cross = -1 -> drawn
    back = ((0.0, 0.0), (0.0, 1.0), (1.0, 0.0))    # reversed -> culled
    assert screen_backface(*front) is False
    assert screen_backface(*back) is True
    # a degenerate (collinear) triangle has zero area -> treated as back-facing
    assert screen_backface((0.0, 0.0), (1.0, 1.0), (2.0, 2.0)) is True


def test_v_nail_has_two_sided_coincident_faces():
    """v_nail.mdl (unlike every other view model) is built with two-sided flat
    faces: pairs of triangles on the SAME three vertices, opposite winding, each
    mapping a different skin half. They are why it needs backface culling -- drawn
    together they z-fight (the shimmering grey). Guards that data assumption."""
    pak = Pak(PAK)
    pal = pak.read("gfx/palette.lmp")
    palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]

    def coincident_pairs(name):
        m = Mdl(pak.read(name), palette)
        seen = {}
        for t in m.tris:
            k = tuple(sorted(t))
            seen[k] = seen.get(k, 0) + 1
        return sum(v - 1 for v in seen.values() if v > 1)

    assert coincident_pairs("progs/v_nail.mdl") == 6, "v_nail fixture changed"
    # the others have none -- which is why only the nailgun showed the bug
    for other in ("progs/v_shot.mdl", "progs/v_nail2.mdl", "progs/v_rock2.mdl"):
        assert coincident_pairs(other) == 0, other


def test_viewmodel_backface_cull_kills_zfight_shimmer():
    """The nailgun's two-sided faces z-fought: as the view shifts sub-pixel, the
    front/back skin halves swap per pixel and shimmer grey. With WinQuake's
    per-triangle backface cull only one of each pair draws, so the model is
    stable. Measure model-region pixel flicker across a small yaw sweep with the
    cull on vs forcibly off and require the cull to cut it by a wide margin."""
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    colormap = pak.read("gfx/colormap.lmp")[:64 * 256]
    b = Bsp(pak.read("maps/e1m1.bsp"))
    r = Renderer(b, palette, colormap)
    r.resize(800, 600)
    nail = Mdl(pak.read("progs/v_nail.mdl"), palette)
    verts = nail.frame_verts(0, 0.0)
    origin, yaw = b.find_spawn()
    eye = (origin[0], origin[1], origin[2] + 22.0)
    styles = [256] * 64

    def model_flicker(cull):
        # disabling the cull reproduces the pre-fix renderer
        if not cull:
            saved = render_mod.screen_backface
            render_mod.screen_backface = lambda a, b_, c: False
        try:
            flips = 0
            prev_fb = prev_mask = None
            for k in range(6):
                dy = k * 0.05
                (fb, w, h), _ = r.render_zbuffer(
                    eye, yaw + dy, 0.0, textured=True, lightstyles=styles,
                    time=0.5, view_model=(nail, verts, eye, (0.0, yaw + dy, 0.0)))
                (bg, _, _), _ = r.render_zbuffer(
                    eye, yaw + dy, 0.0, textured=True, lightstyles=styles, time=0.5)
                fb = bytes(fb); bg = bytes(bg)
                mask = {i for i in range(w * h) if fb[i] != bg[i]}   # model pixels
                if prev_fb is not None:
                    flips += sum(1 for i in (mask & prev_mask) if fb[i] != prev_fb[i])
                prev_fb, prev_mask = fb, mask
            return flips
        finally:
            if not cull:
                render_mod.screen_backface = saved

    off = model_flicker(False)
    on = model_flicker(True)
    assert on * 5 < off, \
        f"backface cull did not quell the nailgun z-fight (flips on={on} off={off})"


def test_pickup_box_texels_floor_negative_coords():
    """The ammo box's side texture pads its unused texels in the top rows; the
    box face maps t from 0 (bottom edge) to -24 (top), so the bottom strip has
    t in (-1, 0). Wrapping that with int() (truncates toward zero) reads texel
    row 0 -- the padding -- as a stray bright line along the box bottom (the
    e1m4 / e1m2 'weird line around the bottom pixel'); floor() reads the last
    row, the real art. Render the e1m4 shells box and assert the production
    sampler floors: it must differ from a forced int()-truncating sampler (which
    reproduces the bug). If the renderer ever reverts to int(), the two match and
    this fails."""
    import math as _math
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    colormap = pak.read("gfx/colormap.lmp")[:64 * 256]
    b = Bsp(pak.read("maps/e1m4.bsp"))
    r = Renderer(b, palette, colormap)
    r.resize(1600, 1200)
    pm = PickupModel(Bsp(pak.read("maps/b_shell0.bsp")), palette)
    box = (-88.0, 2160.0, 1224.0)             # the shells box by the e1m4 spawn
    center = (box[0] + 12, box[1] + 12, box[2] + 12)
    eye = (-160.0, 2210.0, 1264.0)
    dx, dy, dz = center[0] - eye[0], center[1] - eye[1], center[2] - eye[2]
    yaw = _math.degrees(_math.atan2(dy, dx))
    pitch = _math.degrees(_math.atan2(-dz, _math.hypot(dx, dy)))

    def render():
        (fb, w, h), _ = r.render_zbuffer(eye, yaw, pitch, textured=True,
                                         lightstyles=[256] * 64, time=0.5,
                                         bsp_ents=[(pm, box, (0.0, 0.0, 0.0))])
        return bytes(fb), w, h

    fb_prod, w, h = render()
    saved = _math.floor
    _math.floor = lambda v: int(v)            # reproduce the pre-fix truncation
    try:
        fb_int, _, _ = render()
    finally:
        _math.floor = saved
    diff = sum(1 for a, b2 in zip(fb_prod, fb_int) if a != b2)
    assert diff > 500, \
        f"renderer is truncating negative texel coords, not flooring (diff={diff})"


def _renderer():
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    colormap = pak.read("gfx/colormap.lmp")[:64 * 256]
    b = Bsp(pak.read("maps/e1m1.bsp"))
    r = Renderer(b, palette, colormap)
    r.resize(800, 600)                       # -> 200x150 internal framebuffer
    return b, r


def _views(b):
    origin, yaw = b.find_spawn()
    eye = (origin[0], origin[1], origin[2] + 22.0)
    styles = [256] * 64
    flicker = [256] * 64
    flicker[0] = 300                          # exercise _animate_lightmaps
    flicker[1] = 150
    views = []
    for i, dy in enumerate((0.0, 90.0, 180.0, 270.0)):
        views.append((f"tex_yaw{i}", eye, yaw + dy, 0.0, True, styles))
    views.append(("tex_pitch", eye, yaw, 25.0, True, styles))
    views.append(("tex_flicker", eye, yaw, 0.0, True, flicker))
    views.append(("flat_yaw0", eye, yaw, 0.0, False, styles))
    views.append(("flat_yaw2", eye, yaw + 180.0, 0.0, False, styles))
    return views


def _render_view(r, view):
    _name, eye, yaw, pitch, textured, styles = view
    (fb, w, h), _leaf = r.render_zbuffer(eye, yaw, pitch, textured=textured,
                                         lightstyles=styles, time=0.5)
    return bytes(fb), w, h


def regen_goldens():
    b, r = _renderer()
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for view in _views(b):
        fb, w, h = _render_view(r, view)
        path = os.path.join(GOLDEN_DIR, f"zbuf_{view[0]}_{w}x{h}.bin")
        with open(path, "wb") as f:
            f.write(fb)
        print(f"wrote {path}")


def test_golden_frames():
    b, r = _renderer()
    pal = r.palette
    for view in _views(b):
        fb, w, h = _render_view(r, view)
        path = os.path.join(GOLDEN_DIR, f"zbuf_{view[0]}_{w}x{h}.bin")
        assert os.path.exists(path), \
            f"missing golden {path}; run `python test_zbuffer_raster.py --regen`"
        with open(path, "rb") as f:
            ref = f.read()
        assert len(ref) == len(fb) == w * h, (view[0], len(ref), len(fb))
        npix = w * h
        ndiff = 0
        total = 0
        # frames are palette indices; diff in RGB so the tolerances measure
        # visible error, not index distance
        for i in range(npix):
            a = fb[i]
            b2 = ref[i]
            if a != b2:
                ndiff += 1
                pa = pal[a]
                pb = pal[b2]
                total += (abs(pa[0] - pb[0]) + abs(pa[1] - pb[1])
                          + abs(pa[2] - pb[2]))
        frac = ndiff / npix
        mean = total / (npix * 3)
        assert frac <= MAX_DIFF_FRAC, (view[0], f"{frac:.4f} of pixels differ")
        assert mean <= MAX_MEAN_ABS, (view[0], f"mean abs diff {mean:.3f}")
        print(f"  {view[0]}: {frac*100:.2f}% pixels differ, mean {mean:.3f}")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        regen_goldens()
        sys.exit(0)
    test_poly_spans_square()
    test_poly_spans_half_offset()
    test_poly_spans_triangle_matches_edge_functions()
    test_poly_spans_clamps_to_framebuffer()
    test_poly_spans_empty_cases()
    test_poly_spans_no_cracks_or_overlap()
    test_plane_gradients_recovers_linear_attr()
    test_plane_gradients_degenerate_returns_none()
    test_surface_cache_matches_direct_math()
    test_surface_cache_reuse_and_invalidation()
    test_screen_backface_winding()
    test_v_nail_has_two_sided_coincident_faces()
    test_viewmodel_backface_cull_kills_zfight_shimmer()
    test_pickup_box_texels_floor_negative_coords()
    test_golden_frames()
    print("OK")
