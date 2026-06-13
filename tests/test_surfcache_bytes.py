"""Surface-cache live-byte accounting (D_CacheSurface / the 64MB flush bound).

`_surf_cache_bytes` gates the full-cache flush in `_surface_cache`. It must
track the bytes actually *in* the map, not every byte ever built: a dlight
restore pops an entry (apply_dlights) and a +N texture swap overwrites one,
and both must debit the counter. The original code only ever added on build,
so churn inflated the counter to the 64MB bound and flushed a healthy cache --
a one-frame all-surfaces rebuild spike (scache ~= whole frame in perf logs).
This pins the invariant that the counter equals the live map's byte total.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os

os.environ.setdefault("PQ_AUDIO", "0")

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer

PAK = "quake-shareware/id1/pak0.pak"


def _renderer():
    pak = Pak(PAK)
    pal = pak.read("gfx/palette.lmp")
    palette = [(pal[i*3], pal[i*3+1], pal[i*3+2]) for i in range(256)]
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    return bsp, Renderer(bsp, palette, pak.read("gfx/colormap.lmp")[:64*256])


def _live_bytes(rend):
    return sum(len(e[2]) for e in rend._surf_cache_map.values())


def _lit_faces(bsp, rend, n):
    out = []
    for fi in range(len(bsp.faces)):
        rec = rend.face_lm[fi]
        if rec[5] and rec[0] >= 2 and rec[1] >= 2:      # real lightmap
            out.append(fi)
            if len(out) >= n:
                break
    assert out, "no lightmapped faces?"
    return out


def test_counter_tracks_live_bytes_through_churn():
    bsp, rend = _renderer()
    faces = _lit_faces(bsp, rend, 8)

    # build a batch of surfaces
    for fi in faces:
        rend._surface_cache(fi, rend.face_tex[fi])
    assert rend._surf_cache_bytes == _live_bytes(rend), "build accounting"

    # overwrite path: a +N texture swap rebuilds the same key with new bytes.
    # The stale entry must be debited, so the counter must not double-count.
    fi = faces[0]
    w, h, rgb, a, b = rend.face_tex[fi]
    rend.face_tex[fi] = (w, h, bytes(rgb), a, b)         # new tex object, same size
    rend._surface_cache(fi, rend.face_tex[fi])
    assert rend._surf_cache_bytes == _live_bytes(rend), "overwrite accounting"

    # pop path: a dlight marks faces, then a restore frame pops their entries.
    styles = [256] * 64
    cx = cy = cz = 0.0
    vs = [bsp.vertexes[v] for v in rend.face_verts[faces[0]]]
    cx = sum(v[0] for v in vs) / len(vs)
    cy = sum(v[1] for v in vs) / len(vs)
    cz = sum(v[2] for v in vs) / len(vs)
    rend.apply_dlights([(cx, cy, cz, 300.0, 16.0)], styles)
    # build the dlit variants the mark produced
    for fi in list(rend._dlit_faces):
        rend._surface_cache(fi, rend.face_tex[fi])
    assert rend._surf_cache_bytes == _live_bytes(rend), "dlit build accounting"
    rend.apply_dlights([], styles)                       # restore: pops dlit entries
    assert rend._surf_cache_bytes == _live_bytes(rend), "pop accounting"

    assert rend._surf_cache_bytes >= 0


if __name__ == "__main__":
    test_counter_tracks_live_bytes_through_churn()
    print("OK")
