"""Quake alias model (.mdl) reader. Pure stdlib.

MDL is the format for monsters, items, weapons -- anything that animates by
swapping whole vertex sets ("frames"). On-disk layout (little-endian), all
sequential after the 84-byte header:

  header  mdl_t   ident "IDPO", version 6, scale, scale_origin (translate),
                  boundingradius, eyeposition, numskins, skinwidth, skinheight,
                  numverts, numtris, numframes, synctype, flags, size
  skins   per skin: int type (0 single / 1 group); single = w*h palette bytes,
                     group = int n, n float intervals, n*(w*h) byte images
  stverts numverts * (onseam, s, t)            -- texcoords
  tris    numtris  * (facesfront, vertindex[3])
  frames  per frame: int type (0 single / 1 group). A single frame is
                     {bbox(8) name[16]} then numverts trivertx (byte v[3] +
                     lightnormalindex). A group is {int n, bbox(8)} then n float
                     intervals then n single-frame bodies.

A vertex byte triple decodes to a real position as v*scale + scale_origin.
We decode every frame to float verts up front and average the first skin's
colour (for flat-shade tinting), which is all the renderer needs.
"""

import math
import struct

ALIAS_VERSION = 6
IDPO = b"IDPO"
# model effect flags (mdl.h). The client reads these off the model to decide a
# moving entity's particle trail / spin -- they are NOT entity .effects.
EF_ROCKET = 1           # leave a rocket (fire/smoke) trail
EF_GRENADE = 2          # leave a grenade smoke trail
EF_GIB = 4              # leave a blood trail
EF_ROTATE = 8           # model flag: bonus item, spun client-side (cl_main.c)
EF_TRACER = 16          # green split trail (scrag/wizard)
EF_ZOMGIB = 32          # small blood trail (zombie gibs)
EF_TRACER2 = 64         # orange split trail (hellknight)
EF_TRACER3 = 128        # purple trail (vore/voor)

# mdl_t: ident, version, scale[3], origin[3], radius, eye[3], 8 ints, size
_HEADER = struct.Struct("<ii 3f 3f f 3f iiiiiiii f")


def model_flags(data):
    """Read just the mdl flags field from a raw .mdl, without decoding the model.
    Returns 0 if the data isn't a recognisable MDL (so callers can treat it as
    'no trail'). The flags drive client-side trails and item spin."""
    if len(data) < _HEADER.size or data[0:4] != IDPO:
        return 0
    return _HEADER.unpack_from(data, 0)[19]
_TRIVERTX = struct.Struct("<4B")     # v[3], lightnormalindex


class Mdl:
    def __init__(self, data, palette=None):
        h = _HEADER.unpack_from(data, 0)
        if data[0:4] != IDPO:
            raise ValueError(f"not an MDL (ident {data[0:4]!r})")
        if h[1] != ALIAS_VERSION:
            raise ValueError(f"MDL version {h[1]}, expected {ALIAS_VERSION}")
        self.scale = (h[2], h[3], h[4])
        self.origin = (h[5], h[6], h[7])         # scale_origin (translate)
        self.boundingradius = h[8]
        numskins = h[12]
        self.skinwidth = h[13]
        self.skinheight = h[14]
        self.numverts = h[15]
        self.numtris = h[16]
        self.numframes = h[17]
        self.flags = h[19]                       # synctype=h[18], flags=h[19]

        sw, sh, nv, nt, nf = (self.skinwidth, self.skinheight,
                              self.numverts, self.numtris, self.numframes)
        skinbytes = sw * sh
        o = _HEADER.size

        # --- skins: keep the first image's pixels for an average colour ---
        first_skin = None
        for _ in range(numskins):
            typ = struct.unpack_from("<i", data, o)[0]; o += 4
            if typ == 0:                              # ALIAS_SKIN_SINGLE
                img = data[o:o + skinbytes]; o += skinbytes
            else:                                     # ALIAS_SKIN_GROUP
                n = struct.unpack_from("<i", data, o)[0]; o += 4
                o += n * 4                            # intervals
                img = data[o:o + skinbytes]           # first image of the group
                o += n * skinbytes
            if first_skin is None:
                first_skin = img
        self.skin_color = _avg_color(first_skin, palette)
        # full skin kept as raw palette indices for the textured z-buffer path
        # (the rasteriser lights indices through the colormap; no RGB decode)
        self.skin_idx = ((sw, sh, bytes(first_skin))
                         if first_skin is not None and len(first_skin) >= sw * sh
                         else None)

        # --- stverts: (onseam, s, t) texel coords per vertex ---
        stverts = [struct.unpack_from("<3i", data, o + i * 12) for i in range(nv)]
        o += nv * 12

        # --- triangles: vertex indices (self.tris) + per-corner texcoords
        # (self.tri_st). Quake's seam rule: a back-facing triangle using an
        # on-seam vertex samples the right half of the skin (s += skinwidth/2). ---
        half = sw // 2
        self.tris = []
        self.tri_st = []
        for _ in range(nt):
            ff, a, b, c = struct.unpack_from("<4i", data, o); o += 16
            self.tris.append((a, b, c))
            st = []
            for vi in (a, b, c):
                onseam, s, t = stverts[vi]
                if not ff and onseam:
                    s += half
                st.append((s, t))
            self.tri_st.append((st[0], st[1], st[2]))

        # --- frames: decode each to float verts; group = animated sub-frames ---
        sx, sy, sz = self.scale
        ox, oy, oz = self.origin

        def read_frame(o):
            # single-frame body: bbox(8) + name[16], then nv trivertx
            o += 24
            verts = []
            for _ in range(nv):
                vx, vy, vz, _n = _TRIVERTX.unpack_from(data, o); o += 4
                verts.append((vx * sx + ox, vy * sy + oy, vz * sz + oz))
            return o, verts

        self.frames = []         # each slot: (cum_intervals | None, [verts, ...])
        for _ in range(nf):
            typ = struct.unpack_from("<i", data, o)[0]; o += 4
            if typ == 0:                              # ALIAS_SINGLE
                o, verts = read_frame(o)
                self.frames.append((None, [verts]))
            else:                                     # ALIAS_GROUP
                n = struct.unpack_from("<i", data, o)[0]; o += 4
                o += 8                                # group bbox
                cum = list(struct.unpack_from("<%df" % n, data, o)); o += n * 4
                subs = []
                for _ in range(n):
                    o, verts = read_frame(o)
                    subs.append(verts)
                self.frames.append((cum, subs))

    def frame_verts(self, frame, t):
        """Float verts for frame slot `frame` at server time `t` (groups cycle)."""
        if frame < 0 or frame >= len(self.frames):
            frame = 0
        cum, subs = self.frames[frame]
        if cum is None or len(subs) == 1:
            return subs[0]
        total = cum[-1]
        if total <= 0:
            return subs[0]
        tt = math.fmod(t, total)
        for i, end in enumerate(cum):
            if tt < end:
                return subs[i]
        return subs[-1]


def _avg_color(img, palette):
    if not img or palette is None:
        return None
    from collections import Counter
    r = g = b = tot = 0
    for idx, c in Counter(img).items():
        pr, pg, pb = palette[idx]
        r += pr * c; g += pg * c; b += pb * c; tot += c
    return (r / tot, g / tot, b / tot) if tot else None


if __name__ == "__main__":
    import sys
    from .pak import Pak
    pak = Pak("quake-shareware/id1/pak0.pak")
    pal = pak.read("gfx/palette.lmp")
    palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]
    name = sys.argv[1] if len(sys.argv) > 1 else "progs/soldier.mdl"
    m = Mdl(pak.read(name), palette)
    print(f"{name}:")
    print(f"  verts  {m.numverts}")
    print(f"  tris   {m.numtris}")
    print(f"  frames {m.numframes} slots "
          f"({sum(len(s[1]) for s in m.frames)} total sub-frames, "
          f"{sum(1 for s in m.frames if s[0] is not None)} animated groups)")
    print(f"  scale  {tuple(round(x,4) for x in m.scale)}")
    print(f"  origin {tuple(round(x,1) for x in m.origin)}")
    print(f"  radius {m.boundingradius:.1f}")
    print(f"  skin   {m.skinwidth}x{m.skinheight}  avg colour {m.skin_color}")
    # bounds of frame 0
    v = m.frame_verts(0, 0)
    xs = [p[0] for p in v]; ys = [p[1] for p in v]; zs = [p[2] for p in v]
    print(f"  frame0 bounds x[{min(xs):.0f},{max(xs):.0f}] "
          f"y[{min(ys):.0f},{max(ys):.0f}] z[{min(zs):.0f},{max(zs):.0f}]")
