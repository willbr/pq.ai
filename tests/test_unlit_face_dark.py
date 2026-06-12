"""Opaque faces with no lightmap render dark, not full-bright.

A BSP face whose lightofs is -1 has no static lightmap. Quake's R_BuildLightMap
(r_surf.c) only forces full bright for TEX_SPECIAL surfaces (sky / *liquids):
the sky and warp paths ignore lighting. For an ordinary opaque face the LIGHT
tool reached no light on, surf->samples is NULL, blocklights stay 0, and the
surface renders at the darkest colormap row -- dark, not bright.

The old fallback in _build_lightmaps full-brighted EVERY lightofs==-1 face, so
unlit ceilings/recesses (e.g. the dark rock1_2 roof in e1m4 near 85 1396 912)
lit up to 255. This pins the split: sky/turb stay 255, opaque go dark.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer

PAK = "quake-shareware/id1/pak0.pak"


def _renderer(mapname):
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    colormap = pak.read("gfx/colormap.lmp")[:64 * 256]
    b = Bsp(pak.read(f"maps/{mapname}.bsp"))
    return b, Renderer(b, palette, colormap)


def test_opaque_unlit_faces_are_dark_and_special_stay_bright():
    b, r = _renderer("e1m4")
    opaque_unlit = special_unlit = 0
    for fi, f in enumerate(b.faces):
        lightofs = f[5]
        if lightofs != -1:
            continue
        rec = r.face_lm[fi]
        lux = rec[4]
        assert rec[5] is False, f"face {fi}: lightofs=-1 should be has_real=False"
        if r.face_sky[fi] or r.face_turb[fi]:
            # sky / liquid / teleport: drawn full bright (warp & sky paths)
            special_unlit += 1
            assert max(lux) == 255, \
                f"special face {fi} should stay full-bright, got {max(lux)}"
        else:
            # ordinary opaque face the light tool reached no light on -> dark
            opaque_unlit += 1
            assert max(lux) == 0, \
                f"opaque unlit face {fi} should be dark (luxel 0), got {max(lux)}"

    # e1m4 has both kinds; guard against the test silently checking nothing
    assert opaque_unlit > 100, opaque_unlit
    assert special_unlit > 0, special_unlit


def test_specific_e1m4_roof_face_is_dark():
    # face 1905 is a rock1_2 ceiling panel above the spot the bug was filed at;
    # it has no lightmap and was rendering full-bright.
    b, r = _renderer("e1m4")
    assert b.faces[1905][5] == -1
    assert max(r.face_lm[1905][4]) == 0, "e1m4 unlit rock roof must be dark"


if __name__ == "__main__":
    test_opaque_unlit_faces_are_dark_and_special_stay_bright()
    test_specific_e1m4_roof_face_is_dark()
    print("OK")
