"""Wireframe renderer for Quake BSP. Pure stdlib.

Per frame:
  1. find the leaf the camera is in (walk the BSP nodes)
  2. decompress that leaf's PVS -> set of potentially visible leaves
  3. gather those leaves' faces, backface-cull them
  4. dedup edges, transform vertices to camera space (cached per frame)
  5. clip each edge to the near plane, project to screen, cull off-screen
Returns a flat list of (x0, y0, x1, y1) line segments for the UI to draw.

Quake world space is Z-up, right-handed. Camera space: x=right, y=up, z=forward.
"""

import math
import sys

from .perf import PROFILER

sys.setrecursionlimit(20000)   # BSP back-to-front walk can recurse deep

NEAR = 1.0
BACKFACE_EPS = 0.01
MIN_SEG_PX2 = 9.0          # drop segments shorter than 3px (Tk cost, no detail)
MIN_POLY_PX2 = 16.0        # drop polygons smaller than this area (Tk fill cost)

# z-buffer mode renders a real software framebuffer (per-pixel depth test) at
# 1/ZBUF_SCALE of the window, then the UI scales it up. Pure-Python per-pixel
# fill is slow, so a low internal resolution keeps it interactive.
ZBUF_SCALE = 4
ZBUF_BG = (40, 40, 56)     # flat colour where nothing is drawn (past the sky)

# turbulent-surface warp (water, lava, slime, teleporters). Quake offsets each
# texel by a sine of the *other* axis plus time: amplitude 8 texels, phase
# index (coord*0.125 + time) * TURBSCALE, wrapped to the 256-entry table.
_TURBSCALE = 256.0 / (2.0 * math.pi)
_TURBSIN = [8.0 * math.sin(i * 2.0 * math.pi / 256.0) for i in range(256)]
# sky drift: texels/sec each sky layer scrolls. A Quake sky is two stacked
# 128x128 layers -- a transparent-keyed cloud foreground over a background --
# composited into one tile (R_InitSky/R_MakeSky). The two scroll at different
# speeds for parallax; the foreground (clouds) outruns the background.
SKY_BG_SCROLL = 8.0
SKY_FG_SCROLL = 16.0

# lightmap luxels are 0..255; Quake brightens them with overbright bits we don't
# emulate, so a gain keeps lit areas from looking muddy. DEFAULT_LIGHT is what an
# unlit surface / off-map alias model gets so it stays visible.
LIGHT_GAIN = 1.6
DEFAULT_LIGHT = 180
# flat-shaded mode has no texture detail, so it leans on the texture average
# being brightened more (it used a 2.2 directional gain before lighting existed).
FLAT_LIGHT_GAIN = 2.2 / 255.0


def poly_spans(sx, sy, width, height):
    """Scanline x-intervals of a convex screen-space polygon, sampled at pixel
    centres (x+0.5, y+0.5). Returns (y0, spans) where spans[r] = (xl, xr)
    covers row y0+r: pixels xl..xr-1 have their centre inside. Rows clamp to
    [0, height), x bounds to [0, width); a row crossed only outside the window
    yields xl >= xr. The half-open rule (top/left in, bottom/right out) makes
    polygons sharing an edge tile exactly: no cracks, no double-drawn pixels."""
    ceil = math.ceil
    ymin = min(sy)
    ymax = max(sy)
    y0 = ceil(ymin - 0.5)
    if y0 < 0:
        y0 = 0
    y1 = ceil(ymax - 0.5)
    if y1 > height:
        y1 = height
    if y0 >= y1:
        return 0, []
    nr = y1 - y0
    xl = [1e30] * nr
    xr = [-1e30] * nr
    n = len(sx)
    ax, ay = sx[n - 1], sy[n - 1]
    for i in range(n):
        bx, by = sx[i], sy[i]
        if ay != by:
            if ay < by:
                ex0, ey0, ex1, ey1 = ax, ay, bx, by
            else:
                ex0, ey0, ex1, ey1 = bx, by, ax, ay
            ys = ceil(ey0 - 0.5)            # rows whose centre is in [ey0, ey1)
            if ys < y0:
                ys = y0
            ye = ceil(ey1 - 0.5)
            if ye > y1:
                ye = y1
            if ys < ye:
                slope = (ex1 - ex0) / (ey1 - ey0)
                xx = ex0 + (ys + 0.5 - ey0) * slope
                for r in range(ys - y0, ye - y0):
                    if xx < xl[r]:
                        xl[r] = xx
                    if xx > xr[r]:
                        xr[r] = xx
                    xx += slope
        ax, ay = bx, by
    spans = []
    append = spans.append
    int_ = int
    for r in range(nr):
        lf = xl[r]
        rf = xr[r]
        if lf > rf:                          # row never crossed (clipped away)
            append((0, 0))
            continue
        # exact ceil(x - 0.5) without a math.ceil call: int() truncates toward
        # zero (= ceil for negatives, floor for positives), bump when below.
        t = lf - 0.5                         # centres in [lf, rf)
        xli = int_(t)
        if xli < t:
            xli += 1
        if xli < 0:
            xli = 0
        t = rf - 0.5
        xri = int_(t)
        if xri < t:
            xri += 1
        if xri > width:
            xri = width
        append((xli, xri))
    return y0, spans


def plane_gradients(sx, sy, attrs):
    """Screen-space gradients of per-vertex attributes that are linear across
    the projected plane (1/z, u/z, v/z all are). Picks the widest vertex
    triple for numeric stability; returns [(a00, dadx, dady), ...] aligned to
    `attrs` (each a per-vertex list) with attr(x, y) = a00 + dadx*x + dady*y,
    or None when the polygon is degenerate (collinear: spans no plane)."""
    n = len(sx)
    x0, y0 = sx[0], sy[0]
    best = 0.0
    bi = 1
    for k in range(1, n - 1):
        d = (sx[k] - x0) * (sy[k + 1] - y0) - (sx[k + 1] - x0) * (sy[k] - y0)
        if d > best or -d > best:
            best = d if d > 0.0 else -d
            bi = k
    if best < 1e-9:
        return None
    x1, y1 = sx[bi], sy[bi]
    x2, y2 = sx[bi + 1], sy[bi + 1]
    e1x, e1y = x1 - x0, y1 - y0
    e2x, e2y = x2 - x0, y2 - y0
    inv = 1.0 / (e1x * e2y - e1y * e2x)
    out = []
    for a in attrs:
        a0 = a[0]
        d1 = a[bi] - a0
        d2 = a[bi + 1] - a0
        dadx = (d1 * e2y - d2 * e1y) * inv
        dady = (d2 * e1x - d1 * e2x) * inv
        out.append((a0 - dadx * x0 - dady * y0, dadx, dady))
    return out


def lightstyle_values(styles, t):
    """Current brightness (0..550, 256 = normal) for light styles 0..63 from
    {index: animation_string} at time t. Quake cycles the string at 10 Hz and
    maps each character a..z to a scale via 22*(c-'a'); unset styles stay 256."""
    tick = int(t * 10)
    out = [256] * 64
    for s, st in styles.items():
        if 0 <= s < 64 and st:
            out[s] = 22 * (ord(st[tick % len(st)]) - 97)
    return out


def angle_vectors(yaw_deg, pitch_deg, roll_deg=0.0):
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    forward = (cp * cy, cp * sy, -sp)
    right = (sy, -cy, 0.0)
    up = (sp * cy, sp * sy, cp)
    if roll_deg:                    # view roll: spin right/up about forward
        rr = math.radians(roll_deg)
        cr, sr = math.cos(rr), math.sin(rr)
        right, up = (tuple(cr * right[i] - sr * up[i] for i in range(3)),
                     tuple(sr * right[i] + cr * up[i] for i in range(3)))
    return forward, right, up


def model_axes(angles):
    """Standard Quake AngleVectors for an alias model (pitch is negated, as in
    R_AliasSetUpTransform). angles = (pitch, yaw, roll) in degrees."""
    p = math.radians(-angles[0]); y = math.radians(angles[1]); r = math.radians(angles[2])
    sp, cp = math.sin(p), math.cos(p)
    sy, cy = math.sin(y), math.cos(y)
    sr, cr = math.sin(r), math.cos(r)
    forward = (cp * cy, cp * sy, -sp)
    right = (-sr * sp * cy + cr * sy, -sr * sp * sy - cr * cy, -sr * cp)
    up = (cr * sp * cy + sr * sy, cr * sp * sy - sr * cy, cr * cp)
    return forward, right, up


# directional light for flat-shading alias models (matches the world's look)
_AL = (0.35, 0.25, 0.90)
_ALM = math.sqrt(_AL[0] ** 2 + _AL[1] ** 2 + _AL[2] ** 2)
ALIAS_LIGHT = (_AL[0] / _ALM, _AL[1] / _ALM, _AL[2] / _ALM)
ALIAS_GAIN = 2.0           # Quake skin averages are dark; brighten to be visible
WORLD_GAIN = 2.2           # matches Renderer's per-face brightening for the world
# WinQuake's view-model depth hack (R_AliasDrawModel, r_alias.c: ziscale *= 3 for
# cl.viewent): scale only the z-buffer depth (not the screen projection) by this
# for the first-person weapon, so it pushes 3x closer in z and always wins the
# depth test against world geometry, and its own coaxial barrel triangles -- whose
# true depths are near-equal -- separate by 3x and stop shimmering.
VIEWMODEL_ZSCALE = 3.0
# z-buffered point particles (D_DrawParticle, d_part.c): each is a small square
# scaled by distance. RADIUS is the world half-extent fed to the 1/z scale; MAX
# caps the half-size in framebuffer pixels so a point-blank puff can't fill the
# screen (a half of 0 is a single pixel).
PARTICLE_ZBUF_RADIUS = 2.0
PARTICLE_ZBUF_MAX = 3


def _bsp_texture_colors(bsp, palette):
    """Average RGB per miptex of `bsp` via a palette histogram; None where
    unusable. Standalone twin of Renderer._texture_colors, for pickup models."""
    from collections import Counter
    out = []
    for t in bsp.textures:
        if t is None or t[3] is None or palette is None:
            out.append(None)
            continue
        r = g = b = tot = 0
        for idx, c in Counter(t[3]).items():
            pr, pg, pb = palette[idx]
            r += pr * c
            g += pg * c
            b += pb * c
            tot += c
        out.append((r / tot, g / tot, b / tot) if tot else None)
    return out


def _bsp_texture_idx(bsp):
    """Each miptex of `bsp` as (w, h, index_bytes), or None where unusable.
    Standalone twin of Renderer._decode_textures, for texturing pickup models."""
    out = []
    for t in bsp.textures:
        if t is None or t[3] is None:
            out.append(None)
            continue
        _name, w, h, idx = t
        if w <= 0 or h <= 0 or len(idx) < w * h:
            out.append(None)
            continue
        out.append((w, h, bytes(idx)))
    return out


class PickupModel:
    """An external .bsp brush model loaded as a pickup (health box, ammo box).

    These are standalone little Quake BSPs (maps/b_bh25.bsp, maps/b_shell0.bsp,
    ...), not inline '*N' submodels of the world and not .mdl alias models, so
    neither the brush-model nor the alias path drew them. Each is a convex box,
    so we precompute its faces in model-local space and draw it with backface
    culling alone -- no internal BSP walk or face sorting needed.

    self.faces: list of (local_verts, (nx, ny, nz, dist), color_hex, (r,g,b),
                         texrec, s_vec, t_vec) where texrec is (w,h,index_bytes)
                         or None and s_vec/t_vec map a local vertex to texel
                         coords.
    self.mins/self.maxs: model-space bounds, for PVS + painter routing.
    """
    def __init__(self, bsp, palette):
        m0 = bsp.models[0]
        self.mins = tuple(m0["mins"])
        self.maxs = tuple(m0["maxs"])
        tex_rgb = _bsp_texture_colors(bsp, palette)     # average colour (flat mode)
        tex_full = _bsp_texture_idx(bsp)                # texel indices (textured mode)
        lx, ly, lz = ALIAS_LIGHT
        self.faces = []
        self.edges = []                 # (p0, p1) local-space, for wireframe mode
        ff, nf = m0["firstface"], m0["numfaces"]
        for fi in range(ff, ff + nf):
            planenum, side, firstedge, numedges, ti, _lofs, _styles = bsp.faces[fi]
            verts = []
            for k in range(numedges):
                se = bsp.surfedges[firstedge + k]
                vi = bsp.edges[se][0] if se >= 0 else bsp.edges[-se][1]
                verts.append(bsp.vertexes[vi])
            (nx, ny, nz), dist, _ = bsp.planes[planenum]
            if side:
                nx, ny, nz, dist = -nx, -ny, -nz, -dist
            inten = (0.50 + 0.50 * max(0.0, nx * lx + ny * ly + nz * lz)) * WORLD_GAIN
            base = None
            texrec = svec = tvec = None
            if 0 <= ti < len(bsp.texinfo):
                mt = bsp.texinfo[ti][0]
                if 0 <= mt < len(tex_rgb):
                    base = tex_rgb[mt]
                if 0 <= mt < len(tex_full) and tex_full[mt] is not None:
                    texrec = tex_full[mt]
                    svec = bsp.texinfo[ti][2]
                    tvec = bsp.texinfo[ti][3]
            if base is None:
                base = (140.0, 140.0, 140.0)
            r = min(255, int(base[0] * inten))
            g = min(255, int(base[1] * inten))
            b = min(255, int(base[2] * inten))
            self.faces.append((verts, (nx, ny, nz, dist),
                               f"#{r:02x}{g:02x}{b:02x}", (r, g, b),
                               texrec, svec, tvec))
            for k in range(numedges):
                self.edges.append((verts[k], verts[(k + 1) % numedges]))


class Renderer:
    def __init__(self, bsp, palette=None, colormap=None):
        self.bsp = bsp
        self.palette = palette          # list of 256 (r,g,b) for texture colours
        # gfx/colormap.lmp: 64 light rows x 256 palette indices; row 0 is full
        # bright, row 63 darkest (our 0..255 luxel maps to row (255-lux)>>2 --
        # the inversion in id's R_BuildLightMap). The z-buffer mode draws into
        # an 8-bit palette-indexed framebuffer and lights every texel through
        # this table, exactly like WinQuake. Without one (palette-less boots),
        # a no-op identity table keeps the code path alive, just unlit.
        if colormap is None:
            colormap = bytes(range(256)) * 64
        self.colormap = colormap[:64 * 256]
        self._cmap_rows = [self.colormap[i * 256:(i + 1) * 256]
                           for i in range(64)]
        self._idx_cache = {}            # (r,g,b) -> nearest palette index memo
        # background of the z-buffer framebuffer as a palette index
        self._bg_idx = self._nearest_index(ZBUF_BG) if palette else 0
        self.headnode = bsp.models[0]["headnode"]
        self.width = 800
        self.height = 600
        # textured-mode resolution divisor: render at 1/zbuf_scale of the window.
        # An instance attribute (not the module constant) so the console's
        # zbuf_scale cvar can change it live; resize() reads it.
        self.zbuf_scale = ZBUF_SCALE
        # Fixed textured render resolution: when set to (w, h) the z-buffer
        # framebuffer is exactly that size (stretched to the window on present),
        # overriding the zbuf_scale-derived size. None = derive from the window
        # (today's behaviour); the video-options menu sets a fixed mode.
        self.video_res = None
        self.fov = 90.0
        self.backface = True
        self.brushmodels = True     # draw doors/lifts/buttons (submodels 1..N)
        self._update_focal()
        self._setup_zbuf()

        nfaces = len(bsp.faces)
        nedges = len(bsp.edges)
        nverts = len(bsp.vertexes)

        # precompute per-face: ordered abs edge indices, ordered vertex indices
        # (winding), and the outward plane (n, dist)
        self.face_edges = []
        self.face_verts = []
        self.face_plane = []
        for planenum, side, firstedge, numedges, texinfo, _lofs, _styles in bsp.faces:
            eidx = []
            vidx = []
            for k in range(numedges):
                se = bsp.surfedges[firstedge + k]
                eidx.append(abs(se))
                vidx.append(bsp.edges[se][0] if se >= 0 else bsp.edges[-se][1])
            self.face_edges.append(eidx)
            self.face_verts.append(vidx)
            (nx, ny, nz), dist, _ = bsp.planes[planenum]
            if side:
                nx, ny, nz, dist = -nx, -ny, -nz, -dist
            self.face_plane.append((nx, ny, nz, dist))

        # average RGB per texture (from its mip-0 palette indices)
        tex_rgb = self._texture_colors()

        # precompute a flat-shade fill colour per face: texture's average colour
        # modulated by a static directional light. Falls back to grey if a face
        # has no usable texture / no palette was supplied.
        lx, ly, lz = 0.35, 0.25, 0.90
        lm = math.sqrt(lx * lx + ly * ly + lz * lz)
        lx, ly, lz = lx / lm, ly / lm, lz / lm
        # Quake texture averages are very dark (the engine brightens with
        # lightmaps, which we don't apply) -> boost so the scene is visible.
        gain = 2.2
        texinfo = bsp.texinfo
        self.face_color_idx = []        # palette index for the z-buffer flat fill
        self.face_base_rgb = []         # raw texture average, lit per-face by flat mode
        cmap = self.colormap
        for fi, (nx, ny, nz, dist) in enumerate(self.face_plane):
            inten = (0.50 + 0.50 * max(0.0, nx * lx + ny * ly + nz * lz)) * gain
            base = None
            mode_idx = None
            ti = bsp.faces[fi][4]
            if 0 <= ti < len(texinfo):
                mt = texinfo[ti][0]
                if 0 <= mt < len(tex_rgb):
                    base = tex_rgb[mt]
                    mode_idx = self.tex_mode_idx[mt]
            if base is None:
                base = (140.0, 140.0, 140.0)
            self.face_base_rgb.append(base)
            # palette-indexed flat fill: the texture's most common index lit
            # through the colormap. inten (1.1..2.2) maps to luxel 128..255 so
            # the directional shading survives the row quantisation.
            if mode_idx is None:
                mode_idx = self._nearest_index(base)
            lux = int(inten * 116.0)
            if lux > 255:
                lux = 255
            self.face_color_idx.append(cmap[((255 - lux) >> 2) * 256 + mode_idx])

        # full-resolution textures as raw palette indices, for the textured
        # z-buffer rasteriser (lit through the colormap). Aligned to
        # bsp.textures; None where the miptex has no level-0 pixels or no
        # palette was supplied.
        self.tex_idx = self._decode_textures()

        # classify miptexes by Quake's name conventions: sky* scroll, *liquids
        # warp (water/lava/slime/teleport), and +N frames cycle. _classify_tex
        # also builds the animation chains (per-miptex list of frame indices).
        self.is_sky, self.is_turb, self.tex_anim, self.tex_alt = \
            self._classify_textures()

        # per-face texture record for the rasteriser: (w, h, index_bytes,
        # s_vec, t_vec) or None to fall back to flat colour. Plus a per-face
        # integer shade (0..256, 8-bit fixed point) from the same directional
        # light, so textured surfaces still read their facing.
        self.face_tex = []
        self.face_shade = []
        self.face_sky = [False] * nfaces      # scroll the sky texture
        self.face_sky_mt = [-1] * nfaces      # sky face -> its miptex (for tiles)
        self.face_turb = [False] * nfaces     # sine-warp (liquids, teleporters)
        self.face_anim = [None] * nfaces      # [(w,h,idx), ...] frames, or None
        self.face_alt = [None] * nfaces       # alternate frames (entity frame!=0)
        for fi, (nx, ny, nz, dist) in enumerate(self.face_plane):
            self.face_shade.append(int((0.55 + 0.45 * max(0.0,
                                   nx * lx + ny * ly + nz * lz)) * 256))
            rec = None
            ti = bsp.faces[fi][4]
            if 0 <= ti < len(texinfo):
                mt = texinfo[ti][0]
                if 0 <= mt < len(self.tex_idx):
                    self.face_sky[fi] = self.is_sky[mt]
                    if self.is_sky[mt]:
                        self.face_sky_mt[fi] = mt
                    self.face_turb[fi] = self.is_turb[mt]
                    if self.tex_anim[mt] is not None:
                        frames = [self.tex_idx[m] for m in self.tex_anim[mt]
                                  if self.tex_idx[m] is not None]
                        if len(frames) > 1:
                            self.face_anim[fi] = frames
                    if self.tex_alt[mt] is not None:
                        alt = [self.tex_idx[m] for m in self.tex_alt[mt]
                               if self.tex_idx[m] is not None]
                        if alt:
                            self.face_alt[fi] = alt
                    if self.tex_idx[mt] is not None:
                        w, h, idx = self.tex_idx[mt]
                        rec = (w, h, idx, texinfo[ti][2], texinfo[ti][3])
            self.face_tex.append(rec)
        # sky textures split into their two 128x128 layers (R_InitSky); the
        # per-frame composite tile is cached in _make_sky.
        self.sky_split = self._split_sky()
        self._sky_tiles = {}          # mt -> (w, h, tile_bytes)
        self._sky_shift = None        # last (fg, bg) integer shift built at
        # faces whose texture cycles -- the only ones _animate_surfaces touches
        self.anim_faces = [fi for fi in range(nfaces)
                           if self.face_anim[fi] is not None]

        # lit-surface cache (Quake's surface cache, r_surf.c): per face, its
        # texture tiled over the face's extent pre-lit through the colormap,
        # built lazily on first draw and dropped when the lightmap recombines
        # or a +N animation swaps the texture. Each 16-texel luxel cell builds
        # in one bytes.translate through its colormap row.
        self._surf_cache_map = {}
        self._surf_cache_bytes = 0
        self._dlit_faces = set()    # faces brightened by dlights this frame

        # per-face lightmaps from the LIGHTING lump (baked static light). Each
        # entry: (lmw, lmh, smin, tmin, luxels, has_real). For surfaces with a
        # lightmap the luxels come from the BSP (one per 16 texels, the active
        # styles combined by current brightness). Faces with no lightmap (sky,
        # liquids) get a 1x1 map holding their directional shade, so the
        # rasteriser always samples a lightmap and never branches per pixel.
        self._build_lightmaps()

        # per-frame staleness markers (avoid clearing big arrays every frame)
        self.frame = 0
        self.face_frame = [0] * nfaces
        self.face_visframe = [0] * nfaces   # marked from visible leaves (zbuf walk)
        self.edge_frame = [0] * nedges
        self.vert_frame = [0] * nverts
        self.vcache = [None] * nverts

        # vis decompression scratch
        self.vis_row = (len(bsp.leafs) + 7) >> 3

        # BSP parent links + visframe markers, for node-based back-to-front
        # world drawing with PVS culling (flat-shading painter's algorithm)
        self.node_parent = [-1] * len(bsp.nodes)
        self.leaf_parent = [-1] * len(bsp.leafs)
        self.node_visframe = [0] * len(bsp.nodes)
        stack = [(self.headnode, -1)]
        while stack:
            num, par = stack.pop()
            if num < 0:
                self.leaf_parent[-num - 1] = par
                continue
            self.node_parent[num] = par
            ch = bsp.nodes[num][1]
            stack.append((ch[0], num))
            stack.append((ch[1], num))

    def _nearest_index(self, rgb):
        """Nearest palette index to an (r,g,b) -- for the few colours computed
        at runtime (flat-shade fills, background). Memoised; the palette is
        searched linearly only on a miss. Fullbright slots (240..255) are
        skipped: they would glow regardless of lighting."""
        key = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        hit = self._idx_cache.get(key)
        if hit is not None:
            return hit
        pal = self.palette
        if not pal:
            return 0
        r, g, b = key
        best = 0
        bd = 1 << 30
        for i in range(240):
            pr, pg, pb = pal[i]
            d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
            if d < bd:
                bd = d
                best = i
        self._idx_cache[key] = best
        return best

    def _texture_colors(self):
        """Average RGB per miptex via a palette histogram. None where unusable.
        Also fills self.tex_mode_idx: each miptex's most common palette index,
        the flat fill colour of the palette-indexed framebuffer."""
        from collections import Counter
        pal = self.palette
        out = []
        self.tex_mode_idx = []
        for t in self.bsp.textures:
            if t is None or t[3] is None or pal is None:
                out.append(None)
                self.tex_mode_idx.append(None)
                continue
            r = g = b = tot = 0
            hist = Counter(t[3])
            self.tex_mode_idx.append(hist.most_common(1)[0][0] if hist else None)
            for idx, c in hist.items():              # idx -> pixel count
                pr, pg, pb = pal[idx]
                r += pr * c
                g += pg * c
                b += pb * c
                tot += c
            out.append((r / tot, g / tot, b / tot) if tot else None)
        return out

    def _decode_textures(self):
        """Validate each miptex's level-0 image and keep its raw palette
        indices. Returns a list aligned to bsp.textures: (w, h, index_bytes)
        or None where unusable. The rasteriser samples indices and lights them
        through the colormap, so no RGB expansion happens at all."""
        pal = self.palette
        out = []
        for t in self.bsp.textures:
            if t is None or t[3] is None or pal is None:
                out.append(None)
                continue
            _name, w, h, idx = t
            if w <= 0 or h <= 0 or len(idx) < w * h:
                out.append(None)
                continue
            out.append((w, h, bytes(idx)))
        return out

    def _classify_textures(self):
        """Split miptexes by Quake's name conventions and build animation chains.
        Returns (is_sky, is_turb, tex_anim, tex_alt) aligned to bsp.textures:
          - is_sky[mt]:  name starts 'sky'  -> scrolling sky.
          - is_turb[mt]: name starts '*'    -> sine-warped liquid/teleporter.
          - tex_anim[mt]: the frame list this miptex cycles through (the main
            '+0..+9x' sequence or, for an alternate frame, the '+a..+jx' one),
            ordered, or None.
          - tex_alt[mt]: the *other* sequence -- the alternate frames a brush
            entity switches to when its `frame` field is set (Quake's
            R_TextureAnimation / alternate_anims, e.g. a button lighting up).
            None when the group has no alternate. Mirrors model.c: '+0..+9'
            are the main frames, '+a..+j' the alternates."""
        textures = self.bsp.textures
        n = len(textures)
        is_sky = [False] * n
        is_turb = [False] * n
        tex_anim = [None] * n
        tex_alt = [None] * n
        groups = {}                  # base name -> {'main': {i:mt}, 'alt': {i:mt}}
        for mt, t in enumerate(textures):
            if t is None:
                continue
            name = t[0].lower()
            if name.startswith("sky"):
                is_sky[mt] = True
            elif name.startswith("*"):
                is_turb[mt] = True
            elif name.startswith("+") and len(name) >= 2:
                c = name[1]
                g = groups.setdefault(name[2:], {"main": {}, "alt": {}})
                if c.isdigit():
                    g["main"][int(c)] = mt
                elif "a" <= c <= "j":
                    g["alt"][ord(c) - ord("a")] = mt
        for g in groups.values():
            main = [g["main"][k] for k in sorted(g["main"])]
            alt = [g["alt"][k] for k in sorted(g["alt"])]
            for mt in main:
                tex_anim[mt] = main
                if alt:
                    tex_alt[mt] = alt
            for mt in alt:
                tex_anim[mt] = alt
                if main:
                    tex_alt[mt] = main
        return is_sky, is_turb, tex_anim, tex_alt

    def _animate_surfaces(self, t):
        """Swap each +N face to the frame for time t (Quake cycles at 5 Hz).
        Only animated faces are touched; sky/turb need no per-frame state."""
        if not self.anim_faces:
            return
        fidx = int(t * 5.0)
        for fi in self.anim_faces:
            frames = self.face_anim[fi]
            w, h, rgb = frames[fidx % len(frames)]
            old = self.face_tex[fi]
            self.face_tex[fi] = (w, h, rgb, old[3], old[4])

    def brush_face_tex(self, fi, frame, time):
        """The texture record to draw brush face `fi` with. When the owning
        entity's `frame` is set, swap to the face's alternate chain -- Quake's
        R_TextureAnimation, which lights a pressed button (buttons.qc sets
        frame=1 to 'use alternate textures'). Otherwise the base (the world's
        own, already time-animated) record. The texinfo s/t vectors are kept so
        the texture still rides the brush."""
        base = self.face_tex[fi]
        alt = self.face_alt[fi]
        if frame and alt is not None and base is not None:
            w, h, idx = alt[int(time * 5.0) % len(alt)]
            return (w, h, idx, base[3], base[4])
        return base

    def _split_sky(self):
        """Each sky miptex (256x128) split into its two 128x128 layers, à la
        R_InitSky: foreground clouds = the left half (palette index 0 reads as
        transparent), background = the right half. Returns {mt: (fg, bg)} with
        fg/bg as 128*128 index bytes. Skips skies that aren't 256x128."""
        out = {}
        for mt, sky in enumerate(self.is_sky):
            if not sky or self.tex_idx[mt] is None:
                continue
            w, h, idx = self.tex_idx[mt]
            if w < 256 or h < 128:
                continue
            fg = bytearray(128 * 128)
            bg = bytearray(128 * 128)
            for r in range(128):
                src = r * w
                fg[r * 128:(r + 1) * 128] = idx[src:src + 128]
                bg[r * 128:(r + 1) * 128] = idx[src + 128:src + 256]
            out[mt] = (bytes(fg), bytes(bg))
        return out

    def _make_sky(self, time):
        """Composite each split sky into one 128x128 tile for time `t`
        (R_MakeSky): the two layers scrolled at different speeds, the
        foreground's non-zero (opaque) texels overlaid on the background.
        Rebuilt only when an integer texel shift changes -- the sky steps a
        texel at a time, exactly like WinQuake. Returns {mt: (128, 128, tile)}."""
        if not self.sky_split:
            return self._sky_tiles
        fgs = int(time * SKY_FG_SCROLL) & 127
        bgs = int(time * SKY_BG_SCROLL) & 127
        if self._sky_shift == (fgs, bgs) and self._sky_tiles:
            return self._sky_tiles
        self._sky_shift = (fgs, bgs)
        tiles = {}
        for mt, (fg, bg) in self.sky_split.items():
            tile = bytearray(128 * 128)
            for r in range(128):
                fr = ((r + fgs) & 127) << 7
                br = ((r + bgs) & 127) << 7
                out = r << 7
                for c in range(128):
                    px = fg[fr + ((c + fgs) & 127)]
                    tile[out + c] = px if px else bg[br + ((c + bgs) & 127)]
            tiles[mt] = (128, 128, bytes(tile))
        self._sky_tiles = tiles
        return tiles

    def _build_lightmaps(self):
        """Per-face lightmap data from the LIGHTING lump. Fills self.face_lm
        (the live, possibly animated map sampled by the rasteriser) and
        self.face_lm_styles (each face's raw per-style luxel blocks, recombined
        when a style's brightness changes).

        face_lm entry: (lmw, lmh, smin, tmin, luxels, has_real). Real lightmaps
        have one luxel per 16 texels (Quake's CalcSurfaceExtents); faces without
        one (sky, liquids) get a 1x1 map holding their directional shade, so the
        rasteriser samples a lightmap unconditionally. luxels is a bytearray so
        animation can rewrite it in place."""
        bsp = self.bsp
        light = bsp.lightdata
        texinfo = bsp.texinfo
        vertexes = bsp.vertexes
        face_verts = self.face_verts
        nfaces = len(bsp.faces)
        self.face_lm = []
        self.face_lm_styles = []        # per face: [(style_num, block_bytes), ...] | None
        self.style_faces = {}           # style_num -> [face indices using it]
        self.face_light_avg = [0.0] * nfaces   # mean luxel per face, for flat shading
        self.face_lit_hex = [None] * nfaces    # cached lit flat colour
        self.face_lit_L = [-1.0] * nfaces      # the light level that cache was built at
        for fi in range(len(bsp.faces)):
            lightofs = bsp.faces[fi][5]
            styles = bsp.faces[fi][6]
            ti = bsp.faces[fi][4]
            blocks = None
            rec = None
            if lightofs >= 0 and light and 0 <= ti < len(texinfo):
                s0, s1, s2, s3 = texinfo[ti][2]
                t0, t1, t2, t3 = texinfo[ti][3]
                smin = tmin = 1e30
                smax = tmax = -1e30
                for vi in face_verts[fi]:
                    vx, vy, vz = vertexes[vi]
                    s = vx * s0 + vy * s1 + vz * s2 + s3
                    t = vx * t0 + vy * t1 + vz * t2 + t3
                    if s < smin: smin = s
                    if s > smax: smax = s
                    if t < tmin: tmin = t
                    if t > tmax: tmax = t
                bsmin = math.floor(smin / 16); bsmax = math.ceil(smax / 16)
                btmin = math.floor(tmin / 16); btmax = math.ceil(tmax / 16)
                lmw = int(bsmax - bsmin) + 1
                lmh = int(btmax - btmin) + 1
                n = lmw * lmh
                active = [st for st in styles if st != 255]
                if n > 0 and active and lightofs + n * len(active) <= len(light):
                    blocks = []
                    for si, st in enumerate(active):
                        base = lightofs + si * n
                        blocks.append((st, light[base:base + n]))
                        self.style_faces.setdefault(st, []).append(fi)
                    rec = (lmw, lmh, bsmin * 16.0, btmin * 16.0, bytearray(n), True)
            if rec is None:
                # no lightmap == TEX_SPECIAL (sky / liquids / teleport): Quake
                # draws these full-bright. The textured rasteriser samples this
                # 1x1 luxel (255), while flat mode keeps the directional shade.
                shade = min(255, self.face_shade[fi])
                rec = (1, 1, 0.0, 0.0, bytearray((255,)), False)
                self.face_light_avg[fi] = shade        # constant; never recombined
            self.face_lm.append(rec)
            self.face_lm_styles.append(blocks)

        # initial combine at normal brightness (256). _prev_styleval seeds the
        # animation diff to the same baseline, so the first animated frame only
        # rebuilds faces whose styles actually differ from normal.
        normal = [256] * 64
        for fi in range(len(self.face_lm)):
            if self.face_lm_styles[fi] is not None:
                self._combine_face(fi, normal)
        self._prev_styleval = normal[:]

    def _combine_face(self, fi, styleval):
        """Recombine face fi's lightmap luxels for the current style brightnesses
        (in place). luxel = clamp(sum(block * styleval[style]) * gain / 256)."""
        rec = self.face_lm[fi]
        lmw, lmh, buf = rec[0], rec[1], rec[4]
        n = lmw * lmh
        acc = [0] * n
        for style, block in self.face_lm_styles[fi]:
            sv = styleval[style] if style < len(styleval) else 256
            if sv:
                for i in range(n):
                    acc[i] += block[i] * sv
        g = LIGHT_GAIN / 256.0
        tot = 0
        for i in range(n):
            v = int(acc[i] * g)
            if v > 255: v = 255
            buf[i] = v
            tot += v
        self.face_light_avg[fi] = tot / n        # flat mode reads this per face
        self._surf_cache_map.pop(fi, None)       # lit surface is now stale

    def _animate_lightmaps(self, styleval):
        """Rebuild only the faces whose light-style brightness changed this frame
        (constant styles never rebuild, so most faces are touched once)."""
        prev = self._prev_styleval
        dirty = set()
        for style, faces in self.style_faces.items():
            v = styleval[style] if style < len(styleval) else 256
            if v != prev[style]:
                prev[style] = v
                dirty.update(faces)
        for fi in dirty:
            self._combine_face(fi, styleval)

    def apply_dlights(self, dlights, styleval):
        """R_MarkLights + R_AddDynamicLights (r_light.c / r_surf.c): restore
        last frame's dlit faces to their static lightmaps, then walk the BSP
        marking faces each light sphere touches and add its radius falloff
        into their luxels for this frame. dlights: (x, y, z, radius,
        minlight) tuples in world space."""
        if self._dlit_faces:
            for fi in self._dlit_faces:
                if self.face_lm_styles[fi] is not None:
                    self._combine_face(fi, styleval)
            self._dlit_faces.clear()
        if not dlights:
            return
        bsp = self.bsp
        planes, nodes, faces, texinfo = (bsp.planes, bsp.nodes, bsp.faces,
                                         bsp.texinfo)
        for (lx, ly, lz, radius, minl) in dlights:
            marked = []
            stack = [self.headnode]
            while stack:                        # R_MarkLights
                ni = stack.pop()
                if ni < 0:
                    continue
                planenum, children, firstface, numfaces = nodes[ni]
                (nx, ny, nz), pd, _t = planes[planenum]
                dist = lx * nx + ly * ny + lz * nz - pd
                if dist > radius:
                    stack.append(children[0])
                elif dist < -radius:
                    stack.append(children[1])
                else:
                    marked.extend(range(firstface, firstface + numfaces))
                    stack.append(children[0])
                    stack.append(children[1])
            for fi in marked:                   # R_AddDynamicLights
                rec = self.face_lm[fi]
                if not rec[5]:
                    continue                    # sky/liquid: fullbright anyway
                (nx, ny, nz), pd, _t = planes[faces[fi][0]]
                dist = lx * nx + ly * ny + lz * nz - pd
                rad = radius - abs(dist)
                if rad < minl:
                    continue
                thresh = rad - minl
                ix, iy, iz = lx - nx * dist, ly - ny * dist, lz - nz * dist
                ti = faces[fi][4]
                s0, s1, s2, s3 = texinfo[ti][2]
                t0, t1, t2, t3 = texinfo[ti][3]
                lmw, lmh, smin, tmin, buf, _real = rec
                ls = ix * s0 + iy * s1 + iz * s2 + s3 - smin
                lt = ix * t0 + iy * t1 + iz * t2 + t3 - tmin
                touched = False
                for t in range(lmh):
                    td = lt - (t << 4)
                    if td < 0:
                        td = -td
                    base = t * lmw
                    for s in range(lmw):
                        sd = ls - (s << 4)
                        if sd < 0:
                            sd = -sd
                        d = sd + td * 0.5 if sd > td else td + sd * 0.5
                        if d < thresh:
                            v = buf[base + s] + int(rad - d)
                            buf[base + s] = 255 if v > 255 else v
                            touched = True
                if touched:
                    self._surf_cache_map.pop(fi, None)
                    self._dlit_faces.add(fi)

    def _surface_cache(self, fi, rec):
        """Lit surface for face fi (Quake's D_CacheSurface, r_surf.c): the
        texture tiled over the face's full s/t extent, every texel's palette
        index mapped through the colormap row of the luxel covering it. The
        rasteriser then needs one fetch per pixel -- no lightmap sampling,
        wrap or shade math. Cached until the lightmap recombines
        (_combine_face drops the entry) or the +N animation swaps the texture
        (the `is` check below). Returns (cw, ch, index_bytearray, tex); index
        with (t - tmin, s - smin).

        Rows are built with C-level ops: the texture row is tiled across the
        extent by bytes repeat/slice, then each 16-texel luxel cell is lit in
        one bytes.translate through its colormap row."""
        tw, th, tex = rec[0], rec[1], rec[2]
        ent = self._surf_cache_map.get(fi)
        if ent is not None and ent[3] is tex:
            return ent
        lmw, lmh, smin, tmin, lux, _ = self.face_lm[fi]
        cw = lmw << 4
        ch = lmh << 4
        out = bytearray(cw * ch)
        rows = self._cmap_rows
        smin_i = int(smin)
        tmin_i = int(tmin)
        soff = smin_i % tw
        reps = (soff + cw + tw - 1) // tw
        # Blocky lightmap, faithful to WinQuake's D_CacheSurface /
        # R_DrawSurfaceBlock: each 16-texel luxel cell takes one flat shade from
        # its luxel, so each cell is lit in a single C-level bytes.translate
        # through that luxel's colormap row. (The span renderer is the structural
        # port; the lighting matches id's blocky 16-unit blocks.)
        for tc in range(ch):
            trow = ((tmin_i + tc) % th) * tw
            tiled = (tex[trow:trow + tw] * reps)[soff:soff + cw]
            r0 = (tc >> 4) * lmw
            obase = tc * cw
            for lc in range(lmw):
                bright = lux[r0 + lc]                  # nearest luxel, no blend
                tab = rows[(255 - bright) >> 2]
                s = lc << 4
                out[obase + s:obase + s + 16] = tiled[s:s + 16].translate(tab)
        ent = (cw, ch, out, tex)
        self._surf_cache_bytes += len(out)
        if self._surf_cache_bytes > 64 * 1024 * 1024:   # crude bound: flush all
            self._surf_cache_map.clear()
            self._surf_cache_bytes = len(out)
        self._surf_cache_map[fi] = ent
        return ent

    def light_point(self, p):
        """Baked light at world point p: trace straight down to the nearest lit
        surface and read its lightmap (Quake's RecursiveLightPoint). Used to light
        whole alias models by their surroundings. 0..255, DEFAULT_LIGHT if none."""
        if not self.bsp.lightdata:
            return DEFAULT_LIGHT
        r = self._recursive_light(self.headnode, p,
                                  (p[0], p[1], p[2] - 2048.0))
        return r if r >= 0 else DEFAULT_LIGHT

    def _recursive_light(self, node, start, end):
        if node < 0:
            return -1                              # ray reached a leaf, no hit
        bsp = self.bsp
        planenum, children, ff, nf = bsp.nodes[node]
        (nx, ny, nz), dist, _ = bsp.planes[planenum]
        front = start[0] * nx + start[1] * ny + start[2] * nz - dist
        back = end[0] * nx + end[1] * ny + end[2] * nz - dist
        side = front < 0
        if (back < 0) == side:                     # both ends same side: descend it
            return self._recursive_light(children[1] if side else children[0],
                                         start, end)
        frac = front / (front - back)
        mid = (start[0] + (end[0] - start[0]) * frac,
               start[1] + (end[1] - start[1]) * frac,
               start[2] + (end[2] - start[2]) * frac)
        r = self._recursive_light(children[1] if side else children[0], start, mid)
        if r >= 0:                                 # hit nearer surface on the way
            return r
        texinfo = bsp.texinfo
        for fi in range(ff, ff + nf):
            rec = self.face_lm[fi]
            if not rec[5]:                         # no real lightmap on this face
                continue
            ti = bsp.faces[fi][4]
            s0, s1, s2, s3 = texinfo[ti][2]
            t0, t1, t2, t3 = texinfo[ti][3]
            s = mid[0] * s0 + mid[1] * s1 + mid[2] * s2 + s3
            t = mid[0] * t0 + mid[1] * t1 + mid[2] * t2 + t3
            lmw, lmh, smin, tmin, lux, _ = rec
            ds = s - smin; dt = t - tmin
            if ds < 0 or dt < 0 or ds > (lmw - 1) * 16 or dt > (lmh - 1) * 16:
                continue
            return lux[(int(dt) >> 4) * lmw + (int(ds) >> 4)]
        return self._recursive_light(children[0] if side else children[1], mid, end)

    def _update_focal(self):
        self.focal = (self.width / 2) / math.tan(math.radians(self.fov) / 2)

    def _setup_zbuf(self):
        """(Re)allocate the z-buffer mode's framebuffer + depth templates for the
        current window size. _bg_frame is a pre-coloured background to copy each
        frame; _zb_far seeds the depth buffer to 0 (= infinitely far, since we
        store 1/z and keep the larger value)."""
        if self.video_res is not None:
            self.zw, self.zh = self.video_res        # fixed mode (video menu)
        else:
            self.zw = max(1, self.width // self.zbuf_scale)
            self.zh = max(1, self.height // self.zbuf_scale)
        self._bg_frame = bytes((self._bg_idx,)) * (self.zw * self.zh)
        # a plain list, not array('f'): list reads hand back the stored float
        # object, while array('f') boxes a fresh float on every read -- one
        # allocation per depth test in the rasteriser's hot loop.
        self._zb_far = [0.0] * (self.zw * self.zh)
        # span/edge (scanline) occlusion engine for world/brush geometry -- the
        # r_edge.c port. Lazily imported so non-textured boots don't pay for it.
        from .r_edge import EdgeRaster
        self.edges = EdgeRaster(self.zw, self.zh)

    def resize(self, w, h):
        self.width, self.height = w, h
        self._update_focal()
        self._setup_zbuf()

    def project_point(self, origin, yaw, pitch, p):
        """World point -> (screen_x, screen_y, depth), or None if behind the near
        plane / off the depth axis. Used for point sprites (particles); the depth
        lets the caller size the sprite with distance (focal * radius / depth)."""
        forward, right, up = angle_vectors(yaw, pitch)
        dx = p[0] - origin[0]
        dy = p[1] - origin[1]
        dz = p[2] - origin[2]
        cz = dx * forward[0] + dy * forward[1] + dz * forward[2]   # depth
        if cz < NEAR:
            return None
        cx = dx * right[0] + dy * right[1] + dz * right[2]
        cy = dx * up[0] + dy * up[1] + dz * up[2]
        return (self.width / 2 + self.focal * cx / cz,
                self.height / 2 - self.focal * cy / cz, cz)

    # ---- BSP queries ----
    def point_leaf(self, p):
        node = self.headnode
        nodes = self.bsp.nodes
        planes = self.bsp.planes
        px, py, pz = p
        while node >= 0:
            planenum, children, _, _ = nodes[node]
            (nx, ny, nz), dist, _ = planes[planenum]
            d = px * nx + py * ny + pz * nz - dist
            node = children[0] if d >= 0 else children[1]
        return -node - 1   # leaf index

    def box_in_pvs(self, mins, maxs, vis):
        """True if the AABB touches any leaf marked visible in the PVS bitset.
        Walks the world BSP, descending both sides where the box straddles a
        plane (Quake's Mod_BoxLeafnums, short-circuited on the first hit)."""
        nodes = self.bsp.nodes
        planes = self.bsp.planes
        stack = [self.headnode]
        while stack:
            num = stack.pop()
            while num >= 0:
                planenum, children, _, _ = nodes[num]
                (nx, ny, nz), dist, _ = planes[planenum]
                # project the box extents onto the plane normal
                near = (nx * (maxs[0] if nx >= 0 else mins[0]) +
                        ny * (maxs[1] if ny >= 0 else mins[1]) +
                        nz * (maxs[2] if nz >= 0 else mins[2])) - dist
                far = (nx * (mins[0] if nx >= 0 else maxs[0]) +
                       ny * (mins[1] if ny >= 0 else maxs[1]) +
                       nz * (mins[2] if nz >= 0 else maxs[2])) - dist
                if far >= 0:
                    num = children[0]          # fully in front
                elif near < 0:
                    num = children[1]          # fully behind
                else:
                    stack.append(children[1])  # straddle: visit back later
                    num = children[0]
            leafidx = -num - 1
            if leafidx > 0:
                bit = leafidx - 1
                if vis[bit >> 3] & (1 << (bit & 7)):
                    return True
        return False

    def decompress_vis(self, visofs):
        row = self.vis_row
        if visofs < 0:
            return b"\xff" * row
        out = bytearray(row)            # pre-zeroed: missing tail = "not visible"
        data = self.bsp.visdata
        n = len(data)
        o = i = 0
        while o < row:
            # the last leaf's RLE stream is truncated; the original C over-reads
            # into zeroed memory. We stop at the boundary and leave zeros.
            if visofs + i >= n:
                break
            b = data[visofs + i]
            i += 1
            if b:
                out[o] = b
                o += 1
            else:
                if visofs + i >= n:
                    break
                o += data[visofs + i]   # run of zero bytes
                i += 1
        return bytes(out)

    # ---- main entry ----
    def render(self, origin, yaw, pitch, brush_ents=None, alias_ents=None,
               view_model=None, bsp_ents=None, roll=0.0):
        bsp = self.bsp
        self.frame += 1
        frame = self.frame
        forward, right, up = angle_vectors(yaw, pitch, roll)
        ox, oy, oz = origin
        fx, fy, fz = forward
        rx, ry, rz = right
        ux, uy, uz = up

        vertexes = bsp.vertexes
        edges = bsp.edges
        leafs = bsp.leafs
        marks = bsp.marksurfaces
        face_edges = self.face_edges
        face_plane = self.face_plane
        face_frame = self.face_frame
        edge_frame = self.edge_frame
        vert_frame = self.vert_frame
        vcache = self.vcache

        focal = self.focal
        hw = self.width / 2
        hh = self.height / 2
        W, H = self.width, self.height
        backface = self.backface

        leaf = self.point_leaf(origin)
        visofs = leafs[leaf][1]
        vis = self.decompress_vis(visofs)

        # build the list of visible leaf indices from the PVS bitset
        visible_leaves = []
        nleaf = len(leafs)
        for i in range(nleaf - 1):
            if vis[i >> 3] & (1 << (i & 7)):
                visible_leaves.append(i + 1)

        def transform(vi):
            if vert_frame[vi] == frame:
                return vcache[vi]
            vx, vy, vz = vertexes[vi]
            dx, dy, dz = vx - ox, vy - oy, vz - oz
            c = (dx * rx + dy * ry + dz * rz,    # camera x (right)
                 dx * ux + dy * uy + dz * uz,    # camera y (up)
                 dx * fx + dy * fy + dz * fz)    # camera z (forward/depth)
            vcache[vi] = c
            vert_frame[vi] = frame
            return c

        segments = []

        def emit_seg(cax, cay, caz, cbx, cby, cbz):
            # near-plane clip (caz/cbz are depth)
            if caz < NEAR and cbz < NEAR:
                return
            if caz < NEAR:
                t = (NEAR - caz) / (cbz - caz)
                cax += (cbx - cax) * t
                cay += (cby - cay) * t
                caz = NEAR
            elif cbz < NEAR:
                t = (NEAR - cbz) / (caz - cbz)
                cbx += (cax - cbx) * t
                cby += (cay - cby) * t
                cbz = NEAR

            x0 = hw + cax * focal / caz
            y0 = hh - cay * focal / caz
            x1 = hw + cbx * focal / cbz
            y1 = hh - cby * focal / cbz

            # cheap off-screen reject (both ends past one edge)
            if (x0 < 0 and x1 < 0) or (x0 > W and x1 > W):
                return
            if (y0 < 0 and y1 < 0) or (y0 > H and y1 > H):
                return

            dxp = x1 - x0
            dyp = y1 - y0
            if dxp * dxp + dyp * dyp < MIN_SEG_PX2:
                return                  # sub-pixel: not worth a Tk line draw
            segments.append((x0, y0, x1, y1))

        def emit_face(fi):
            if face_frame[fi] == frame:
                return
            face_frame[fi] = frame
            if backface:
                nx, ny, nz, dist = face_plane[fi]
                if ox * nx + oy * ny + oz * nz - dist <= BACKFACE_EPS:
                    return
            for ei in face_edges[fi]:
                if edge_frame[ei] == frame:
                    continue
                edge_frame[ei] = frame
                a, b = edges[ei]
                cax, cay, caz = transform(a)
                cbx, cby, cbz = transform(b)
                emit_seg(cax, cay, caz, cbx, cby, cbz)

        # same, but for a brush-model entity translated by (ofx, ofy, ofz). The
        # shared vertex cache can't be used (the offset differs per entity), so
        # vertices are transformed inline.
        def emit_face_ofs(fi, ofx, ofy, ofz):
            if face_frame[fi] == frame:
                return
            face_frame[fi] = frame
            if backface:
                nx, ny, nz, dist = face_plane[fi]
                if (ox - ofx) * nx + (oy - ofy) * ny + (oz - ofz) * nz - dist <= BACKFACE_EPS:
                    return
            for ei in face_edges[fi]:
                if edge_frame[ei] == frame:
                    continue
                edge_frame[ei] = frame
                a, b = edges[ei]
                ax, ay, az = vertexes[a]
                bx, by, bz = vertexes[b]
                dax, day, daz = ax + ofx - ox, ay + ofy - oy, az + ofz - oz
                dbx, dby, dbz = bx + ofx - ox, by + ofy - oy, bz + ofz - oz
                emit_seg(dax * rx + day * ry + daz * rz,
                         dax * ux + day * uy + daz * uz,
                         dax * fx + day * fy + daz * fz,
                         dbx * rx + dby * ry + dbz * rz,
                         dbx * ux + dby * uy + dbz * uz,
                         dbx * fx + dby * fy + dbz * fz)

        # world (model 0): only the PVS-visible leaves' surfaces
        for li in visible_leaves:
            _, _, firstmark, nummark = leafs[li]
            for m in range(firstmark, firstmark + nummark):
                emit_face(marks[m])

        # brush-model entities (doors, lifts, buttons), each translated by its
        # current origin. When no entity list is given, fall back to drawing every
        # submodel at rest (standalone / no QC server).
        if self.brushmodels:
            if brush_ents is None:
                brush_ents = [(i, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0)
                              for i in range(1, len(bsp.models))]
            for mi, (ofx, ofy, ofz), _ang, _fr in brush_ents:
                md = bsp.models[mi]
                mn, mx = md["mins"], md["maxs"]
                mins = (mn[0] + ofx, mn[1] + ofy, mn[2] + ofz)
                maxs = (mx[0] + ofx, mx[1] + ofy, mx[2] + ofz)
                if not self.box_in_pvs(mins, maxs, vis):
                    continue
                ff = md["firstface"]
                for fi in range(ff, ff + md["numfaces"]):
                    emit_face_ofs(fi, ofx, ofy, ofz)

        # alias (.mdl) entities as triangle wireframe
        if alias_ents:
            for mdl, verts, org, ang in alias_ents:
                r = mdl.boundingradius
                if not self.box_in_pvs((org[0] - r, org[1] - r, org[2] - r),
                                       (org[0] + r, org[1] + r, org[2] + r), vis):
                    continue
                ox_e, oy_e, oz_e = org
                afwd, arr, aup = model_axes(ang)
                afx, afy, afz = afwd
                arx, ary, arz = arr
                aux, auy, auz = aup
                cam = []
                for vx, vy, vz in verts:
                    wx = ox_e + vx * afx - vy * arx + vz * aux
                    wy = oy_e + vx * afy - vy * ary + vz * auy
                    wz = oz_e + vx * afz - vy * arz + vz * auz
                    dx, dy, dz = wx - ox, wy - oy, wz - oz
                    cam.append((dx * rx + dy * ry + dz * rz,
                                dx * ux + dy * uy + dz * uz,
                                dx * fx + dy * fy + dz * fz))
                for a, b, c in mdl.tris:
                    ca, cb, cc = cam[a], cam[b], cam[c]
                    emit_seg(ca[0], ca[1], ca[2], cb[0], cb[1], cb[2])
                    emit_seg(cb[0], cb[1], cb[2], cc[0], cc[1], cc[2])
                    emit_seg(cc[0], cc[1], cc[2], ca[0], ca[1], ca[2])

        # external .bsp pickups (health/ammo boxes) as edge wireframe
        if bsp_ents:
            for pm, org, ang in bsp_ents:
                ofx, ofy, ofz = org
                mn, mx = pm.mins, pm.maxs
                if not self.box_in_pvs((ofx + mn[0], ofy + mn[1], ofz + mn[2]),
                                       (ofx + mx[0], ofy + mx[1], ofz + mx[2]), vis):
                    continue
                for (ax, ay, az), (bx, by, bz) in pm.edges:
                    d0x, d0y, d0z = ax + ofx - ox, ay + ofy - oy, az + ofz - oz
                    d1x, d1y, d1z = bx + ofx - ox, by + ofy - oy, bz + ofz - oz
                    emit_seg(d0x * rx + d0y * ry + d0z * rz,
                             d0x * ux + d0y * uy + d0z * uz,
                             d0x * fx + d0y * fy + d0z * fz,
                             d1x * rx + d1y * ry + d1z * rz,
                             d1x * ux + d1y * uy + d1z * uz,
                             d1x * fx + d1y * fy + d1z * fz)

        # first-person weapon view model: fixed to the camera, no PVS cull
        if view_model:
            mdl, verts, org, ang = view_model
            ox_e, oy_e, oz_e = org
            afwd, arr, aup = model_axes(ang)
            afx, afy, afz = afwd
            arx, ary, arz = arr
            aux, auy, auz = aup
            cam = []
            for vx, vy, vz in verts:
                wx = ox_e + vx * afx - vy * arx + vz * aux
                wy = oy_e + vx * afy - vy * ary + vz * auy
                wz = oz_e + vx * afz - vy * arz + vz * auz
                dx, dy, dz = wx - ox, wy - oy, wz - oz
                cam.append((dx * rx + dy * ry + dz * rz,
                            dx * ux + dy * uy + dz * uz,
                            dx * fx + dy * fy + dz * fz))
            for a, b, c in mdl.tris:
                ca, cb, cc = cam[a], cam[b], cam[c]
                emit_seg(ca[0], ca[1], ca[2], cb[0], cb[1], cb[2])
                emit_seg(cb[0], cb[1], cb[2], cc[0], cc[1], cc[2])
                emit_seg(cc[0], cc[1], cc[2], ca[0], ca[1], ca[2])

        return segments, leaf

    def render_shaded(self, origin, yaw, pitch, brush_ents=None, alias_ents=None,
                      view_model=None, bsp_ents=None, lightstyles=None,
                      roll=0.0):
        """Flat-shaded polygons, back-to-front (painter's algorithm via the BSP).
        Each world/brush face is filled with its texture average modulated by the
        baked lightmap's mean level for that face (so it darkens in shadow and
        animates with light styles), instead of a static directional shade.
        Returns (polys, leaf) where each poly is (flat_xy_coords, fill_color).
        alias_ents: (mdl, model_space_verts, origin, angles) for .mdl entities.
        bsp_ents: (PickupModel, origin, angles) for external .bsp pickups."""
        bsp = self.bsp
        self.frame += 1
        frame = self.frame
        if lightstyles is not None:                   # animate flickering lights
            self._animate_lightmaps(lightstyles)
        forward, right, up = angle_vectors(yaw, pitch, roll)
        ox, oy, oz = origin
        fx, fy, fz = forward
        rx, ry, rz = right
        ux, uy, uz = up

        vertexes = bsp.vertexes
        leafs = bsp.leafs
        marks = bsp.marksurfaces
        nodes = bsp.nodes
        planes = bsp.planes
        face_verts = self.face_verts
        face_plane = self.face_plane
        face_base_rgb = self.face_base_rgb
        face_light_avg = self.face_light_avg
        face_lit_hex = self.face_lit_hex
        face_lit_L = self.face_lit_L
        face_frame = self.face_frame
        vert_frame = self.vert_frame
        vcache = self.vcache
        focal = self.focal
        hw = self.width / 2
        hh = self.height / 2
        backface = self.backface

        def lit_color(fi):
            # texture average * the face's mean baked light. Cached per face and
            # only recomputed when that light level changes (i.e. on animation).
            L = face_light_avg[fi]
            if L == face_lit_L[fi]:
                return face_lit_hex[fi]
            br, bg, bb = face_base_rgb[fi]
            f = L * FLAT_LIGHT_GAIN
            r = int(br * f); g = int(bg * f); b = int(bb * f)
            if r > 255: r = 255
            if g > 255: g = 255
            if b > 255: b = 255
            hexc = f"#{r:02x}{g:02x}{b:02x}"
            face_lit_hex[fi] = hexc
            face_lit_L[fi] = L
            return hexc
        # camera-space clip planes (a*x+b*y+c*z+d >= 0 is inside): near + the
        # four view-frustum sides, so clipped polygons project on-screen and Tk
        # never has to fill a huge off-screen area.
        tanx = hw / focal
        tany = hh / focal
        clip_planes = ((0.0, 0.0, 1.0, -NEAR),
                       (1.0, 0.0, tanx, 0.0), (-1.0, 0.0, tanx, 0.0),
                       (0.0, 1.0, tany, 0.0), (0.0, -1.0, tany, 0.0))

        leaf = self.point_leaf(origin)
        # in a solid leaf (noclip in the void) there's no PVS -> show everything
        if leafs[leaf][0] == -2 or leafs[leaf][1] < 0:
            vis = b"\xff" * self.vis_row
        else:
            vis = self.decompress_vis(leafs[leaf][1])

        def transform(vi):
            if vert_frame[vi] == frame:
                return vcache[vi]
            vx, vy, vz = vertexes[vi]
            dx, dy, dz = vx - ox, vy - oy, vz - oz
            c = (dx * rx + dy * ry + dz * rz,
                 dx * ux + dy * uy + dz * uz,
                 dx * fx + dy * fy + dz * fz)
            vcache[vi] = c
            vert_frame[vi] = frame
            return c

        polys = []

        def project_poly(poly, color):
            # Sutherland-Hodgman clip against near + the 4 frustum sides, project,
            # drop tiny far polys (Tk fill cost), append.
            for a, b, c, d in clip_planes:
                out = []
                A = poly[-1]
                da = a * A[0] + b * A[1] + c * A[2] + d
                for B in poly:
                    db = a * B[0] + b * B[1] + c * B[2] + d
                    if da >= 0:
                        out.append(A)
                    if (da >= 0) != (db >= 0):
                        t = da / (da - db)
                        out.append((A[0] + (B[0] - A[0]) * t,
                                    A[1] + (B[1] - A[1]) * t,
                                    A[2] + (B[2] - A[2]) * t))
                    A, da = B, db
                poly = out
                if len(poly) < 3:
                    return

            flat = []
            for cx, cy, cz in poly:
                flat.append(hw + cx * focal / cz)
                flat.append(hh - cy * focal / cz)

            area2 = 0.0                                  # shoelace area
            px, py = flat[-2], flat[-1]
            for i in range(0, len(flat), 2):
                qx, qy = flat[i], flat[i + 1]
                area2 += px * qy - qx * py
                px, py = qx, qy
            if -MIN_POLY_PX2 < area2 < MIN_POLY_PX2:
                return
            polys.append((flat, color))

        def emit_face_poly(fi):
            if face_frame[fi] == frame:
                return
            face_frame[fi] = frame
            nx, ny, nz, dist = face_plane[fi]
            if ox * nx + oy * ny + oz * nz - dist <= BACKFACE_EPS:
                return                                  # backface
            project_poly([transform(vi) for vi in face_verts[fi]], lit_color(fi))

        # offset-aware face emit for a brush-model entity at (ofx, ofy, ofz)
        def emit_face_ofs(fi, ofx, ofy, ofz):
            if face_frame[fi] == frame:
                return
            face_frame[fi] = frame
            nx, ny, nz, dist = face_plane[fi]
            if (ox - ofx) * nx + (oy - ofy) * ny + (oz - ofz) * nz - dist <= BACKFACE_EPS:
                return
            pts = []
            for vi in face_verts[fi]:
                vx, vy, vz = vertexes[vi]
                dx, dy, dz = vx + ofx - ox, vy + ofy - oy, vz + ofz - oz
                pts.append((dx * rx + dy * ry + dz * rz,
                            dx * ux + dy * uy + dz * uz,
                            dx * fx + dy * fy + dz * fz))
            project_poly(pts, lit_color(fi))

        def emit_alias(item):
            # alias (.mdl) entity: rotate model verts by angles, translate to the
            # entity origin, draw triangles back-to-front (no backface cull -- a
            # closed mesh painted far-to-near self-occludes correctly).
            verts = item["verts"]
            ox_e, oy_e, oz_e = item["origin"]
            afwd, arr, aup = model_axes(item["angles"])
            afx, afy, afz = afwd
            arx, ary, arz = arr
            aux, auy, auz = aup
            cam = []
            wpos = []
            for vx, vy, vz in verts:
                wx = ox_e + vx * afx - vy * arx + vz * aux
                wy = oy_e + vx * afy - vy * ary + vz * auy
                wz = oz_e + vx * afz - vy * arz + vz * auz
                dx, dy, dz = wx - ox, wy - oy, wz - oz
                cam.append((dx * rx + dy * ry + dz * rz,
                            dx * ux + dy * uy + dz * uz,
                            dx * fx + dy * fy + dz * fz))
                wpos.append((wx, wy, wz))

            base = item["mdl"].skin_color or (150.0, 150.0, 150.0)
            br, bg, bb = base
            lx, ly, lz = ALIAS_LIGHT
            tris = item["mdl"].tris
            # back-to-front by triangle centroid depth (camera z)
            order = sorted(range(len(tris)),
                           key=lambda ti: -(cam[tris[ti][0]][2] + cam[tris[ti][1]][2]
                                            + cam[tris[ti][2]][2]))
            for ti in order:
                a, b, c = tris[ti]
                ca, cb, cc = cam[a], cam[b], cam[c]
                if ca[2] < NEAR and cb[2] < NEAR and cc[2] < NEAR:
                    continue
                # world-space face normal for shading (abs dot: sign-independent)
                ax, ay, az = wpos[a]
                ux1, uy1, uz1 = wpos[b][0] - ax, wpos[b][1] - ay, wpos[b][2] - az
                vx1, vy1, vz1 = wpos[c][0] - ax, wpos[c][1] - ay, wpos[c][2] - az
                nx = uy1 * vz1 - uz1 * vy1
                ny = uz1 * vx1 - ux1 * vz1
                nz = ux1 * vy1 - uy1 * vx1
                nl = math.sqrt(nx * nx + ny * ny + nz * nz)
                inten = (0.5 + 0.5 * abs((nx * lx + ny * ly + nz * lz) / nl)) * ALIAS_GAIN \
                    if nl else 0.6 * ALIAS_GAIN
                r = min(255, int(br * inten))
                g = min(255, int(bg * inten))
                bl = min(255, int(bb * inten))
                project_poly([ca, cb, cc], f"#{r:02x}{g:02x}{bl:02x}")

        # external .bsp pickup (health/ammo box): a convex brush model with its
        # own geometry. Backface-cull each face (so only the front shows) and
        # project it offset to the entity origin -- like emit_face_ofs but over
        # the pickup's own faces instead of the world's.
        def emit_bsp_model(item):
            pm = item["pickup"]
            ofx, ofy, ofz = item["origin"]
            clx, cly, clz = ox - ofx, oy - ofy, oz - ofz
            for verts, (nx, ny, nz, dist), color, _rgb, _tr, _s, _t in pm.faces:
                if clx * nx + cly * ny + clz * nz - dist <= BACKFACE_EPS:
                    continue
                pts = []
                for vx, vy, vz in verts:
                    dx, dy, dz = vx + ofx - ox, vy + ofy - oy, vz + ofz - oz
                    pts.append((dx * rx + dy * ry + dz * rz,
                                dx * ux + dy * uy + dz * uz,
                                dx * fx + dy * fy + dz * fz))
                project_poly(pts, color)

        def emit_model(item):
            if "mdl" in item:
                emit_alias(item)
                return
            if "pickup" in item:
                emit_bsp_model(item)
                return
            # a brush model has its own BSP sub-tree; walk it far-child-first so
            # its faces paint back-to-front (correct even when non-convex). The
            # plane sides are tested in the model's own space (camera - offset).
            ofx, ofy, ofz = item["ofs"]
            cox, coy, coz = ox - ofx, oy - ofy, oz - ofz

            def rec(num):
                if num < 0:
                    return
                planenum, children, ff, nf = nodes[num]
                (nx, ny, nz), dist, _ = planes[planenum]
                if cox * nx + coy * ny + coz * nz - dist >= 0:
                    rec(children[1])                    # far side first
                    for fi in range(ff, ff + nf):
                        emit_face_ofs(fi, ofx, ofy, ofz)
                    rec(children[0])                    # near side last
                else:
                    rec(children[0])
                    for fi in range(ff, ff + nf):
                        emit_face_ofs(fi, ofx, ofy, ofz)
                    rec(children[1])
            rec(item["headnode"])

        def emit_models(mlist):
            if len(mlist) > 1:              # several at one depth -> sort by centre
                def keyf(it):
                    mn, mx = it["mins"], it["maxs"]
                    cx = (mn[0] + mx[0]) * 0.5
                    cy = (mn[1] + mx[1]) * 0.5
                    cz = (mn[2] + mx[2]) * 0.5
                    return -((cx - ox) * fx + (cy - oy) * fy + (cz - oz) * fz)
                mlist = sorted(mlist, key=keyf)
            for it in mlist:
                emit_model(it)

        # collect the visible brush-model entities (doors, lifts, buttons), each
        # carrying its current origin offset. No entity list -> every submodel at
        # rest (standalone / no QC server).
        pending = []
        if self.brushmodels:
            if brush_ents is None:
                brush_ents = [(i, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0)
                              for i in range(1, len(bsp.models))]
            for mi, ofs, _ang, _fr in brush_ents:
                md = bsp.models[mi]
                ofx, ofy, ofz = ofs
                mn, mx = md["mins"], md["maxs"]
                mins = (mn[0] + ofx, mn[1] + ofy, mn[2] + ofz)
                maxs = (mx[0] + ofx, mx[1] + ofy, mx[2] + ofz)
                if self.box_in_pvs(mins, maxs, vis):
                    cx = (mins[0] + maxs[0]) * 0.5
                    cy = (mins[1] + maxs[1]) * 0.5
                    cz = (mins[2] + maxs[2]) * 0.5
                    pending.append({"headnode": md["headnodes"][0],
                                    "mins": mins, "maxs": maxs, "ofs": ofs,
                                    "center": (cx, cy, cz)})

        # alias (.mdl) entities -- monsters, items. Woven into the same painter's
        # walk by a bounding cube (origin +/- the model radius), like brush models.
        if alias_ents:
            for mdl, verts, org, ang in alias_ents:
                r = mdl.boundingradius
                mins = (org[0] - r, org[1] - r, org[2] - r)
                maxs = (org[0] + r, org[1] + r, org[2] + r)
                if self.box_in_pvs(mins, maxs, vis):
                    pending.append({"mdl": mdl, "verts": verts, "origin": org,
                                    "angles": ang, "mins": mins, "maxs": maxs,
                                    "center": org})

        # external .bsp pickups (health/ammo boxes), routed by their centre like
        # the other entities so the painter draws them at their true depth.
        if bsp_ents:
            for pm, org, ang in bsp_ents:
                mn, mx = pm.mins, pm.maxs
                mins = (org[0] + mn[0], org[1] + mn[1], org[2] + mn[2])
                maxs = (org[0] + mx[0], org[1] + mx[1], org[2] + mx[2])
                if self.box_in_pvs(mins, maxs, vis):
                    cx = (mins[0] + maxs[0]) * 0.5
                    cy = (mins[1] + maxs[1]) * 0.5
                    cz = (mins[2] + maxs[2]) * 0.5
                    pending.append({"pickup": pm, "origin": org, "angles": ang,
                                    "mins": mins, "maxs": maxs,
                                    "center": (cx, cy, cz)})

        # PVS: mark every node on the path from each visible leaf up to the root
        node_visframe = self.node_visframe
        node_parent = self.node_parent
        leaf_parent = self.leaf_parent
        for i in range(len(leafs) - 1):
            if vis[i >> 3] & (1 << (i & 7)):
                p = leaf_parent[i + 1]
                while p >= 0 and node_visframe[p] != frame:
                    node_visframe[p] = frame
                    p = node_parent[p]

        # node-based back-to-front walk. Each world face lies on exactly one node,
        # so it is drawn once at its true depth (correct painter's order, unlike
        # leaf-marksurface drawing which mis-orders faces spanning leaves).
        #
        # Entities (brush + alias models) are routed by a single reference point
        # -- their centre -- down to the leaf they occupy, and drawn at that
        # leaf's position in the back-to-front order. Classifying by the model's
        # bounding box instead would send any box straddling a plane to that node
        # to be drawn "on" it; since model boxes are large they straddle high in
        # the tree, painting the model at an essentially arbitrary depth (behind
        # or in front of the world depending on tree shape). The centre point has
        # one well-defined side at every node, so the model lands at its true
        # depth. (A model intersecting a wall can still mis-sort -- the standard
        # painter's limit without a z-buffer -- but free-standing ones are right.)
        def walk(num, models):
            if num < 0 or node_visframe[num] != frame:
                emit_models(models)                       # leaf or PVS-culled
                return
            planenum, children, ff, nf = nodes[num]
            (nx, ny, nz), dist, _ = planes[planenum]
            if models:
                front, back = [], []
                for md in models:
                    cx, cy, cz = md["center"]
                    (front if cx * nx + cy * ny + cz * nz - dist >= 0
                     else back).append(md)
            else:
                front = back = ()
            if ox * nx + oy * ny + oz * nz - dist >= 0:   # camera in front
                walk(children[1], back)                   # far = back side
                for fi in range(ff, ff + nf):
                    emit_face_poly(fi)
                walk(children[0], front)                  # near = front side
            else:
                walk(children[0], front)                  # far = front side
                for fi in range(ff, ff + nf):
                    emit_face_poly(fi)
                walk(children[1], back)

        PROFILER.begin("raster")        # projection/clip of the visible geometry
        walk(self.headnode, pending)

        # first-person weapon view model: drawn last (no z-buffer -> draw order
        # is occlusion), so it always paints on top of the world. No PVS cull.
        if view_model:
            mdl, verts, org, ang = view_model
            emit_alias({"mdl": mdl, "verts": verts, "origin": org, "angles": ang})
        PROFILER.end("raster")

        return polys, leaf

    def render_zbuffer(self, origin, yaw, pitch, brush_ents=None, alias_ents=None,
                       view_model=None, bsp_ents=None, textured=True,
                       lightstyles=None, time=0.0, roll=0.0, sprites=None,
                       particles=None):
        """True per-pixel z-buffered software rasteriser. World/brush faces are
        perspective-correct texture-mapped (textured=True) or flat-shaded; both
        resolve occlusion with a 1/z depth buffer (no painter's ordering, so
        intersecting geometry no longer mis-sorts). Returns ((framebuffer, w,
        h), leaf); the buffer is 8-bit palette indices, one byte per pixel,
        lit through gfx/colormap.lmp exactly like WinQuake -- the UI expands
        it through the palette (tk) or blits it as an 8bpp DIB (gdi32)."""
        bsp = self.bsp
        self.frame += 1
        frame = self.frame
        forward, right, up = angle_vectors(yaw, pitch, roll)
        ox, oy, oz = origin
        fx, fy, fz = forward
        rx, ry, rz = right
        ux, uy, uz = up

        vertexes = bsp.vertexes
        leafs = bsp.leafs
        marks = bsp.marksurfaces
        face_verts = self.face_verts
        face_plane = self.face_plane
        face_color_idx = self.face_color_idx
        face_tex = self.face_tex
        cmap = self.colormap
        cmap_rows = self._cmap_rows
        nearest = self._nearest_index
        face_lm = self.face_lm
        face_sky = self.face_sky
        face_sky_mt = self.face_sky_mt
        face_turb = self.face_turb
        sky_tiles = self._make_sky(time)          # composited sky layers, by mt
        face_frame = self.face_frame
        vert_frame = self.vert_frame
        vcache = self.vcache

        from .r_edge import NORMAL, SKY, TURB
        iw, ih = self.zw, self.zh
        focal = self.focal * iw / self.width          # focal scaled to the small fb
        hw = iw * 0.5
        hh = ih * 0.5
        fb = bytearray(self._bg_frame)                # fresh background to draw over
        zb = self._zb_far[:]                          # depth = 1/z, 0 == far away

        if lightstyles is not None:                   # animate flickering lights
            self._animate_lightmaps(lightstyles)
        if textured:                                  # cycle +N animated textures
            self._animate_surfaces(time)

        leaf = self.point_leaf(origin)
        if leafs[leaf][0] == -2 or leafs[leaf][1] < 0:
            vis = b"\xff" * self.vis_row
        else:
            vis = self.decompress_vis(leafs[leaf][1])

        visible_leaves = []
        for i in range(len(leafs) - 1):
            if vis[i >> 3] & (1 << (i & 7)):
                visible_leaves.append(i + 1)

        def transform(vi):
            if vert_frame[vi] == frame:
                return vcache[vi]
            vx, vy, vz = vertexes[vi]
            dx, dy, dz = vx - ox, vy - oy, vz - oz
            c = (dx * rx + dy * ry + dz * rz,
                 dx * ux + dy * uy + dz * uz,
                 dx * fx + dy * fy + dz * fz)
            vcache[vi] = c
            vert_frame[vi] = frame
            return c

        def raster_poly(cam, ci, zscale=1.0):
            # flat-shaded convex polygon filled with palette index ci:
            # near-plane clip (z >= NEAR), project, then scanline spans -- 1/z
            # is linear in screen space, so each row steps it with one add per
            # pixel (no per-pixel edge tests). The framebuffer is one index
            # byte per pixel, so the depth index doubles as the fb offset.
            # zscale biases only the depth (the view model passes >1 to win the
            # z-test), leaving the screen projection on the true 1/z.
            out = []
            A = cam[-1]; da = A[2] - NEAR
            for B in cam:
                db = B[2] - NEAR
                if da >= 0.0:
                    out.append(A)
                if (da >= 0.0) != (db >= 0.0):
                    t = da / (da - db)
                    out.append((A[0] + (B[0] - A[0]) * t,
                                A[1] + (B[1] - A[1]) * t,
                                A[2] + (B[2] - A[2]) * t))
                A, da = B, db
            if len(out) < 3:
                return
            sx = []; sy = []; siz = []
            for vx, vy, vz in out:
                iz = 1.0 / vz
                sx.append(hw + vx * focal * iz)
                sy.append(hh - vy * focal * iz)
                siz.append(iz * zscale)
            grads = plane_gradients(sx, sy, (siz,))
            if grads is None:
                return                          # degenerate sliver: invisible
            z00, zdx, zdy = grads[0]
            y, spans = poly_spans(sx, sy, iw, ih)
            zbl = zb; fbl = fb; iwl = iw        # locals: avoid LOAD_DEREF per px
            for xli, xri in spans:
                if xli < xri:
                    iz = z00 + zdx * (xli + 0.5) + zdy * (y + 0.5)
                    row = y * iwl
                    for idx in range(row + xli, row + xri):
                        if iz > zbl[idx]:
                            zbl[idx] = iz
                            fbl[idx] = ci
                        iz += zdx
                y += 1

        def raster_poly_tex(cam, rec, lm, zscale=1.0):
            # textured convex polygon. cam: list of (cx, cy, cz, u, v). Near-clip
            # (z >= NEAR) interpolating u,v too, project, then scanline spans:
            # 1/z, u/z, v/z are linear in screen space, so each row steps them
            # with one add per pixel and recovers perspective-correct texels via
            # u,v = (u/z)/(1/z). The texel is modulated by the lightmap luxel
            # covering it (lm = (lmw, lmh, smin, tmin, luxels), sampled per px).
            # zscale biases 1/z, u/z and v/z together: the depth written to the
            # z-buffer is scaled (the view model passes >1 to win the test) while
            # the projection stays on the true 1/z and the texel recovery is
            # unchanged (zscale cancels in u/z over 1/z).
            tw, th, tex = rec[0], rec[1], rec[2]
            lmw, lmh, lsmin, ltmin, lux = lm[0], lm[1], lm[2], lm[3], lm[4]
            out = []
            A = cam[-1]; da = A[2] - NEAR
            for B in cam:
                db = B[2] - NEAR
                if da >= 0.0:
                    out.append(A)
                if (da >= 0.0) != (db >= 0.0):
                    t = da / (da - db)
                    out.append((A[0] + (B[0] - A[0]) * t,
                                A[1] + (B[1] - A[1]) * t,
                                A[2] + (B[2] - A[2]) * t,
                                A[3] + (B[3] - A[3]) * t,
                                A[4] + (B[4] - A[4]) * t))
                A, da = B, db
            if len(out) < 3:
                return
            sx = []; sy = []; siz = []; suz = []; svz = []
            for cx, cy, cz, u, v in out:
                iz = 1.0 / cz
                sx.append(hw + cx * focal * iz)    # project on the true 1/z
                sy.append(hh - cy * focal * iz)
                ziz = iz * zscale                  # depth biased; cancels in u,v
                siz.append(ziz)
                suz.append(u * ziz)            # u/z, linear in screen space
                svz.append(v * ziz)
            grads = plane_gradients(sx, sy, (siz, suz, svz))
            if grads is None:
                return                          # degenerate sliver: invisible
            (z00, zdx, zdy), (u00, udx, udy), (v00, vdx, vdy) = grads
            y, spans = poly_spans(sx, sy, iw, ih)
            zbl = zb; fbl = fb; iwl = iw; int_ = int   # locals beat LOAD_DEREF/GLOBAL
            flat_lm = lmw == 1 and lmh == 1            # sky / alias / pickups:
            rowtab = cmap_rows[(255 - lux[0]) >> 2]    # shade constant per poly
            for xli, xri in spans:
                if xli < xri:
                    xc = xli + 0.5; yc = y + 0.5
                    iz = z00 + zdx * xc + zdy * yc
                    uoz = u00 + udx * xc + udy * yc
                    voz = v00 + vdx * xc + vdy * yc
                    row = y * iwl
                    if flat_lm:
                        for idx in range(row + xli, row + xri):
                            if iz > zbl[idx]:
                                z = 1.0 / iz
                                zbl[idx] = iz
                                fbl[idx] = rowtab[tex[int_(voz * z) % th * tw
                                                      + int_(uoz * z) % tw]]
                            iz += zdx
                            uoz += udx
                            voz += vdx
                        y += 1
                        continue
                    for idx in range(row + xli, row + xri):
                        if iz > zbl[idx]:
                            z = 1.0 / iz
                            u = uoz * z
                            v = voz * z
                            lc = int_((u - lsmin) * 0.0625)
                            if lc < 0: lc = 0
                            elif lc >= lmw: lc = lmw - 1
                            lr = int_((v - ltmin) * 0.0625)
                            if lr < 0: lr = 0
                            elif lr >= lmh: lr = lmh - 1
                            sh = lux[lr * lmw + lc]      # lightmap luxel 0..255
                            zbl[idx] = iz
                            fbl[idx] = cmap[(255 - sh & 252) << 6
                                            | tex[int_(v) % th * tw + int_(u) % tw]]
                        iz += zdx
                        uoz += udx
                        voz += vdx
                y += 1

        # --- world/brush surface emission into the span/edge engine ---
        # These mirror the raster_poly* fills above, but split into two halves:
        # an emit_* that near-clips, projects, computes the 1/z (and u/z, v/z)
        # gradients and registers the screen polygon with the EdgeRaster, and a
        # fill closure (attached to the surface) that the scan() pass calls per
        # span. The fill is write-only -- no `if iz > zb[idx]` test -- because the
        # surface stack already resolved occlusion; it still WRITES 1/z so alias
        # models / particles drawn afterward occlude against the world. All world
        # and brush surfaces share key 0, so the stack orders them purely by 1/z
        # with id's NEARZI_FUDGE tie-break (r_edge.c:488) -- which fixes coplanar
        # lift/wall shimmer deterministically (no per-pixel float-depth ties).
        edges = self.edges

        def emit_cached(pts, sc, csmin, ctmin):
            cw, ch, cache = sc[0], sc[1], sc[2]
            out = []
            A = pts[-1]; da = A[2] - NEAR
            for B in pts:
                db = B[2] - NEAR
                if da >= 0.0:
                    out.append(A)
                if (da >= 0.0) != (db >= 0.0):
                    t = da / (da - db)
                    out.append((A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t,
                                A[2] + (B[2] - A[2]) * t, A[3] + (B[3] - A[3]) * t,
                                A[4] + (B[4] - A[4]) * t))
                A, da = B, db
            if len(out) < 3:
                return
            sx = []; sy = []; siz = []; suz = []; svz = []
            for cx, cy, cz, u, v in out:
                iz = 1.0 / cz
                sx.append(hw + cx * focal * iz)
                sy.append(hh - cy * focal * iz)
                siz.append(iz)
                suz.append((u - csmin) * iz)   # cache-space u/z, v/z
                svz.append((v - ctmin) * iz)
            grads = plane_gradients(sx, sy, (siz, suz, svz))
            if grads is None:
                return
            (z00, zdx, zdy), (u00, udx, udy), (v00, vdx, vdy) = grads
            surf = edges.add_surface(0, NORMAL, (z00, zdx, zdy), list(zip(sx, sy)))

            cwm = cw - 1; chm = ch - 1

            def fill(u, v, count):
                zbl = zb; fbl = fb; int_ = int
                xc = u + 0.5; yc = v + 0.5
                iz = z00 + zdx * xc + zdy * yc
                uoz = u00 + udx * xc + udy * yc
                voz = v00 + vdx * xc + vdy * yc
                row = v * iw
                for idx in range(row + u, row + u + count):
                    z = 1.0 / iz
                    # clamp to the cache extent: the engine's span boundaries can
                    # round a sub-pixel past the projected polygon, sampling just
                    # outside the finite surface cache (WinQuake clamps to bbextents)
                    cu = int_(uoz * z)
                    if cu < 0: cu = 0
                    elif cu > cwm: cu = cwm
                    cv = int_(voz * z)
                    if cv < 0: cv = 0
                    elif cv > chm: cv = chm
                    zbl[idx] = iz
                    fbl[idx] = cache[cv * cw + cu]
                    iz += zdx; uoz += udx; voz += vdx
            surf.fill = fill

        def emit_tex(pts, rec, lm, flags=NORMAL):
            tw, th, tex = rec[0], rec[1], rec[2]
            lmw, lmh, lsmin, ltmin, lux = lm[0], lm[1], lm[2], lm[3], lm[4]
            out = []
            A = pts[-1]; da = A[2] - NEAR
            for B in pts:
                db = B[2] - NEAR
                if da >= 0.0:
                    out.append(A)
                if (da >= 0.0) != (db >= 0.0):
                    t = da / (da - db)
                    out.append((A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t,
                                A[2] + (B[2] - A[2]) * t, A[3] + (B[3] - A[3]) * t,
                                A[4] + (B[4] - A[4]) * t))
                A, da = B, db
            if len(out) < 3:
                return
            sx = []; sy = []; siz = []; suz = []; svz = []
            for cx, cy, cz, u, v in out:
                iz = 1.0 / cz
                sx.append(hw + cx * focal * iz)
                sy.append(hh - cy * focal * iz)
                siz.append(iz)
                suz.append(u * iz)
                svz.append(v * iz)
            grads = plane_gradients(sx, sy, (siz, suz, svz))
            if grads is None:
                return
            (z00, zdx, zdy), (u00, udx, udy), (v00, vdx, vdy) = grads
            surf = edges.add_surface(0, flags, (z00, zdx, zdy), list(zip(sx, sy)))
            if lmw == 1 and lmh == 1:                  # flat (sky / no lightmap)
                rowtab = cmap_rows[(255 - lux[0]) >> 2]

                def fill(u, v, count):
                    zbl = zb; fbl = fb; int_ = int
                    xc = u + 0.5; yc = v + 0.5
                    iz = z00 + zdx * xc + zdy * yc
                    uoz = u00 + udx * xc + udy * yc
                    voz = v00 + vdx * xc + vdy * yc
                    row = v * iw
                    for idx in range(row + u, row + u + count):
                        z = 1.0 / iz
                        zbl[idx] = iz
                        fbl[idx] = rowtab[tex[int_(voz * z) % th * tw + int_(uoz * z) % tw]]
                        iz += zdx; uoz += udx; voz += vdx
            else:                                      # per-pixel lightmap

                def fill(u, v, count):
                    zbl = zb; fbl = fb; int_ = int
                    xc = u + 0.5; yc = v + 0.5
                    iz = z00 + zdx * xc + zdy * yc
                    uoz = u00 + udx * xc + udy * yc
                    voz = v00 + vdx * xc + vdy * yc
                    row = v * iw
                    for idx in range(row + u, row + u + count):
                        z = 1.0 / iz
                        u_ = uoz * z; v_ = voz * z
                        lc = int_((u_ - lsmin) * 0.0625)
                        if lc < 0: lc = 0
                        elif lc >= lmw: lc = lmw - 1
                        lr = int_((v_ - ltmin) * 0.0625)
                        if lr < 0: lr = 0
                        elif lr >= lmh: lr = lmh - 1
                        sh = lux[lr * lmw + lc]
                        zbl[idx] = iz
                        fbl[idx] = cmap[(255 - sh & 252) << 6
                                        | tex[int_(v_) % th * tw + int_(u_) % tw]]
                        iz += zdx; uoz += udx; voz += vdx
            surf.fill = fill

        def emit_turb(pts, rec):
            tw, th, tex = rec[0], rec[1], rec[2]
            sintab = _TURBSIN; scale = _TURBSCALE; tt = time
            out = []
            A = pts[-1]; da = A[2] - NEAR
            for B in pts:
                db = B[2] - NEAR
                if da >= 0.0:
                    out.append(A)
                if (da >= 0.0) != (db >= 0.0):
                    t = da / (da - db)
                    out.append((A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t,
                                A[2] + (B[2] - A[2]) * t, A[3] + (B[3] - A[3]) * t,
                                A[4] + (B[4] - A[4]) * t))
                A, da = B, db
            if len(out) < 3:
                return
            sx = []; sy = []; siz = []; suz = []; svz = []
            for cx, cy, cz, u, v in out:
                iz = 1.0 / cz
                sx.append(hw + cx * focal * iz)
                sy.append(hh - cy * focal * iz)
                siz.append(iz)
                suz.append(u * iz)
                svz.append(v * iz)
            grads = plane_gradients(sx, sy, (siz, suz, svz))
            if grads is None:
                return
            (z00, zdx, zdy), (u00, udx, udy), (v00, vdx, vdy) = grads
            surf = edges.add_surface(0, TURB, (z00, zdx, zdy), list(zip(sx, sy)))

            def fill(u, v, count):
                zbl = zb; fbl = fb; int_ = int
                xc = u + 0.5; yc = v + 0.5
                iz = z00 + zdx * xc + zdy * yc
                uoz = u00 + udx * xc + udy * yc
                voz = v00 + vdx * xc + vdy * yc
                row = v * iw
                for idx in range(row + u, row + u + count):
                    z = 1.0 / iz
                    u_ = uoz * z; v_ = voz * z
                    su2 = u_ + sintab[int_((v_ * 0.125 + tt) * scale) & 255]
                    sv2 = v_ + sintab[int_((u_ * 0.125 + tt) * scale) & 255]
                    zbl[idx] = iz
                    fbl[idx] = tex[int_(sv2) % th * tw + int_(su2) % tw]
                    iz += zdx; uoz += udx; voz += vdx
            surf.fill = fill

        def emit_flat(pts, ci):
            out = []
            A = pts[-1]; da = A[2] - NEAR
            for B in pts:
                db = B[2] - NEAR
                if da >= 0.0:
                    out.append(A)
                if (da >= 0.0) != (db >= 0.0):
                    t = da / (da - db)
                    out.append((A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t,
                                A[2] + (B[2] - A[2]) * t))
                A, da = B, db
            if len(out) < 3:
                return
            sx = []; sy = []; siz = []
            for vx, vy, vz in out:
                iz = 1.0 / vz
                sx.append(hw + vx * focal * iz)
                sy.append(hh - vy * focal * iz)
                siz.append(iz)
            grads = plane_gradients(sx, sy, (siz,))
            if grads is None:
                return
            z00, zdx, zdy = grads[0]
            surf = edges.add_surface(0, NORMAL, (z00, zdx, zdy), list(zip(sx, sy)))

            def fill(u, v, count):
                zbl = zb; fbl = fb
                iz = z00 + zdx * (u + 0.5) + zdy * (v + 0.5)
                row = v * iw
                for idx in range(row + u, row + u + count):
                    zbl[idx] = iz
                    fbl[idx] = ci
                    iz += zdx
            surf.fill = fill

        def emit_face(fi, pts, rec):
            # dispatch a world/brush face to the right emitter: warped liquid,
            # scrolled sky, the lit-surface cache (real lightmaps -- nearly all
            # world geometry), or per-pixel lightmap sampling as the fallback.
            if face_turb[fi]:
                emit_turb(pts, rec)
            elif face_sky[fi]:
                # the two sky layers, composited and scrolled into one 128-tile
                # (no doubling); falls back to the raw miptex if it wasn't split.
                tile = sky_tiles.get(face_sky_mt[fi])
                srec = (tile[0], tile[1], tile[2], rec[3], rec[4]) if tile else rec
                emit_tex(pts, srec, face_lm[fi], SKY)
            else:
                lm = face_lm[fi]
                if lm[5]:
                    emit_cached(pts, self._surface_cache(fi, rec), lm[2], lm[3])
                else:
                    emit_tex(pts, rec, lm)

        def raster_alias(mdl, verts, org, ang, zscale=1.0):
            # rotate model verts into world, transform to camera, flat-shade each
            # triangle by its world normal (matches render_shaded's emit_alias).
            # zscale biases the z-buffer depth (>1 for the view model).
            ox_e, oy_e, oz_e = org
            afwd, arr, aup = model_axes(ang)
            afx, afy, afz = afwd
            arx, ary, arz = arr
            aux, auy, auz = aup
            cam = []; wpos = []
            for vx, vy, vz in verts:
                wx = ox_e + vx * afx - vy * arx + vz * aux
                wy = oy_e + vx * afy - vy * ary + vz * auy
                wz = oz_e + vx * afz - vy * arz + vz * auz
                dx, dy, dz = wx - ox, wy - oy, wz - oz
                cam.append((dx * rx + dy * ry + dz * rz,
                            dx * ux + dy * uy + dz * uz,
                            dx * fx + dy * fy + dz * fz))
                wpos.append((wx, wy, wz))
            base = mdl.skin_color or (150.0, 150.0, 150.0)
            br, bg, bb = base
            lx, ly, lz = ALIAS_LIGHT
            for a, b, c in mdl.tris:
                ax, ay, az = wpos[a]
                ux1, uy1, uz1 = wpos[b][0] - ax, wpos[b][1] - ay, wpos[b][2] - az
                vx1, vy1, vz1 = wpos[c][0] - ax, wpos[c][1] - ay, wpos[c][2] - az
                nx = uy1 * vz1 - uz1 * vy1
                ny = uz1 * vx1 - ux1 * vz1
                nz = ux1 * vy1 - uy1 * vx1
                nl = math.sqrt(nx * nx + ny * ny + nz * nz)
                inten = (0.5 + 0.5 * abs((nx * lx + ny * ly + nz * lz) / nl)) * ALIAS_GAIN \
                    if nl else 0.6 * ALIAS_GAIN
                r = min(255, int(br * inten))
                g = min(255, int(bg * inten))
                bl = min(255, int(bb * inten))
                raster_poly([cam[a], cam[b], cam[c]], nearest((r, g, bl)), zscale)

        def raster_alias_tex(mdl, verts, org, ang, zscale=1.0):
            # textured alias model: skin-mapped per triangle, lit by the baked
            # light sampled at the model's origin (so a monster in a dark room is
            # dark) modulated by each triangle's facing.
            rec = mdl.skin_idx                       # (skinw, skinh, index_bytes)
            wl = self.light_point(org)               # 0..255 ambient from the world
            ox_e, oy_e, oz_e = org
            afwd, arr, aup = model_axes(ang)
            afx, afy, afz = afwd
            arx, ary, arz = arr
            aux, auy, auz = aup
            cam = []; wpos = []
            for vx, vy, vz in verts:
                wx = ox_e + vx * afx - vy * arx + vz * aux
                wy = oy_e + vx * afy - vy * ary + vz * auy
                wz = oz_e + vx * afz - vy * arz + vz * auz
                dx, dy, dz = wx - ox, wy - oy, wz - oz
                cam.append((dx * rx + dy * ry + dz * rz,
                            dx * ux + dy * uy + dz * uz,
                            dx * fx + dy * fy + dz * fz))
                wpos.append((wx, wy, wz))
            lx, ly, lz = ALIAS_LIGHT
            tris = mdl.tris; tri_st = mdl.tri_st
            for ti in range(len(tris)):
                a, b, c = tris[ti]
                (s0, t0), (s1, t1), (s2, t2) = tri_st[ti]
                ax, ay, az = wpos[a]
                ux1, uy1, uz1 = wpos[b][0] - ax, wpos[b][1] - ay, wpos[b][2] - az
                vx1, vy1, vz1 = wpos[c][0] - ax, wpos[c][1] - ay, wpos[c][2] - az
                nx = uy1 * vz1 - uz1 * vy1
                ny = uz1 * vx1 - ux1 * vz1
                nz = ux1 * vy1 - uy1 * vx1
                nl = math.sqrt(nx * nx + ny * ny + nz * nz)
                shf = 0.6 + 0.4 * abs((nx * lx + ny * ly + nz * lz) / nl) if nl else 0.8
                shi = int(wl * shf)                  # world light * facing
                if shi > 255:
                    shi = 255
                ca, cb, cc = cam[a], cam[b], cam[c]
                # a 1x1 lightmap carries the per-triangle light into raster_poly_tex
                lm = (1, 1, 0.0, 0.0, bytes((shi,)))
                raster_poly_tex([(ca[0], ca[1], ca[2], s0, t0),
                                 (cb[0], cb[1], cb[2], s1, t1),
                                 (cc[0], cc[1], cc[2], s2, t2)], rec, lm, zscale)

        def emit_world_face(fi):
            nx, ny, nz, dist = face_plane[fi]
            if ox * nx + oy * ny + oz * nz - dist <= BACKFACE_EPS:
                return
            rec = face_tex[fi] if textured else None
            if rec is not None:
                (s0, s1, s2, s3) = rec[3]
                (t0, t1, t2, t3) = rec[4]
                cam = []
                for vi in face_verts[fi]:
                    c = transform(vi)
                    vx, vy, vz = vertexes[vi]
                    cam.append((c[0], c[1], c[2],
                                vx * s0 + vy * s1 + vz * s2 + s3,
                                vx * t0 + vy * t1 + vz * t2 + t3))
                emit_face(fi, cam, rec)
            else:
                emit_flat([transform(vi) for vi in face_verts[fi]],
                          face_color_idx[fi])

        PROFILER.begin("raster")        # per-pixel fill of all visible geometry
        # world (model 0): mark the visible leaves' surfaces and their ancestor
        # nodes, then walk the BSP near-side-first drawing marked faces (id's
        # R_MarkLeaves + R_RecursiveWorldNode, gl_rsurf.c -- glquake is the
        # z-buffered renderer). Drawing front-to-back doesn't change the image
        # (the depth test resolves either way); it makes occluded pixels fail
        # the test on the cheap path instead of being textured then overdrawn.
        face_visframe = self.face_visframe
        node_visframe = self.node_visframe
        node_parent = self.node_parent
        leaf_parent = self.leaf_parent
        edges.begin_frame()             # reset the span/edge engine for this frame
        for li in visible_leaves:
            _, _, firstmark, nummark = leafs[li]
            for m in range(firstmark, firstmark + nummark):
                face_visframe[marks[m]] = frame
            p = leaf_parent[li]
            while p >= 0 and node_visframe[p] != frame:
                node_visframe[p] = frame
                p = node_parent[p]

        nodes = bsp.nodes
        planes = bsp.planes

        def walk_front(num):
            while num >= 0 and node_visframe[num] == frame:
                planenum, children, ff, nf = nodes[num]
                (nx, ny, nz), dist, _ = planes[planenum]
                if ox * nx + oy * ny + oz * nz - dist >= 0:
                    near, far = children
                else:
                    far, near = children
                walk_front(near)
                for fi in range(ff, ff + nf):
                    if face_visframe[fi] == frame and face_frame[fi] != frame:
                        face_frame[fi] = frame
                        emit_world_face(fi)
                num = far                   # tail-iterate down the far side

        walk_front(self.headnode)

        # brush-model entities (doors, lifts, buttons), each offset to its origin
        if self.brushmodels:
            if brush_ents is None:
                brush_ents = [(i, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0)
                              for i in range(1, len(bsp.models))]
            for mi, (ofx, ofy, ofz), _ang, efr in brush_ents:
                md = bsp.models[mi]
                mn, mx = md["mins"], md["maxs"]
                mins = (mn[0] + ofx, mn[1] + ofy, mn[2] + ofz)
                maxs = (mx[0] + ofx, mx[1] + ofy, mx[2] + ofz)
                if not self.box_in_pvs(mins, maxs, vis):
                    continue
                ff = md["firstface"]
                for fi in range(ff, ff + md["numfaces"]):
                    if face_frame[fi] == frame:
                        continue
                    face_frame[fi] = frame
                    nx, ny, nz, dist = face_plane[fi]
                    if (ox - ofx) * nx + (oy - ofy) * ny + (oz - ofz) * nz - dist <= BACKFACE_EPS:
                        continue
                    # entity frame set -> alternate textures (pressed button)
                    rec = self.brush_face_tex(fi, efr, time) if textured else None
                    if rec is not None:
                        (s0, s1, s2, s3) = rec[3]
                        (t0, t1, t2, t3) = rec[4]
                        pts = []
                        for vi in face_verts[fi]:
                            vx, vy, vz = vertexes[vi]
                            dx, dy, dz = vx + ofx - ox, vy + ofy - oy, vz + ofz - oz
                            # UVs use the model-local vertex (texture rides the brush)
                            pts.append((dx * rx + dy * ry + dz * rz,
                                        dx * ux + dy * uy + dz * uz,
                                        dx * fx + dy * fy + dz * fz,
                                        vx * s0 + vy * s1 + vz * s2 + s3,
                                        vx * t0 + vy * t1 + vz * t2 + t3))
                        emit_face(fi, pts, rec)
                    else:
                        pts = []
                        for vi in face_verts[fi]:
                            vx, vy, vz = vertexes[vi]
                            dx, dy, dz = vx + ofx - ox, vy + ofy - oy, vz + ofz - oz
                            pts.append((dx * rx + dy * ry + dz * rz,
                                        dx * ux + dy * uy + dz * uz,
                                        dx * fx + dy * fy + dz * fz))
                        emit_flat(pts, face_color_idx[fi])
        # resolve world + brush occlusion in one scanline sweep, then fill each
        # surviving span (write-only -- the stack already ordered them).
        for surf in edges.scan():
            fh = surf.fill
            if fh is not None:
                for (su, sv, scount) in surf.spans:
                    fh(su, sv, scount)

        # alias (.mdl) entities -- monsters, items
        if alias_ents:
            for mdl, verts, org, ang in alias_ents:
                r = mdl.boundingradius
                if not self.box_in_pvs((org[0] - r, org[1] - r, org[2] - r),
                                       (org[0] + r, org[1] + r, org[2] + r), vis):
                    continue
                if textured and mdl.skin_idx is not None:
                    raster_alias_tex(mdl, verts, org, ang)
                else:
                    raster_alias(mdl, verts, org, ang)

        # external .bsp pickups (health/ammo/explosive boxes): convex, backface-
        # cull faces. Texture-mapped (textured=True) like the world, otherwise
        # flat-shaded -- both lit by the baked world light at the box origin so a
        # box in a dark room reads dark, modulated per face by its facing.
        if bsp_ents:
            alx, aly, alz = ALIAS_LIGHT
            for pm, org, ang in bsp_ents:
                mn, mx = pm.mins, pm.maxs
                ofx, ofy, ofz = org
                if not self.box_in_pvs((ofx + mn[0], ofy + mn[1], ofz + mn[2]),
                                       (ofx + mx[0], ofy + mx[1], ofz + mx[2]), vis):
                    continue
                wl = self.light_point(org) if textured else 0       # 0..255 ambient
                clx, cly, clz = ox - ofx, oy - ofy, oz - ofz
                for verts, (nx, ny, nz, dist), _color, (r, g, b), texrec, svec, tvec in pm.faces:
                    if clx * nx + cly * ny + clz * nz - dist <= BACKFACE_EPS:
                        continue
                    if textured and texrec is not None:
                        s0, s1, s2, s3 = svec
                        t0, t1, t2, t3 = tvec
                        pts = []
                        for vx, vy, vz in verts:
                            dx, dy, dz = vx + ofx - ox, vy + ofy - oy, vz + ofz - oz
                            pts.append((dx * rx + dy * ry + dz * rz,
                                        dx * ux + dy * uy + dz * uz,
                                        dx * fx + dy * fy + dz * fz,
                                        vx * s0 + vy * s1 + vz * s2 + s3,
                                        vx * t0 + vy * t1 + vz * t2 + t3))
                        shf = 0.6 + 0.4 * abs(nx * alx + ny * aly + nz * alz)
                        shi = int(wl * shf)
                        if shi > 255:
                            shi = 255
                        lm = (1, 1, 0.0, 0.0, bytes((shi,)))
                        raster_poly_tex(pts, texrec, lm)
                    else:
                        pts = []
                        for vx, vy, vz in verts:
                            dx, dy, dz = vx + ofx - ox, vy + ofy - oy, vz + ofz - oz
                            pts.append((dx * rx + dy * ry + dz * rz,
                                        dx * ux + dy * uy + dz * uz,
                                        dx * fx + dy * fy + dz * fz))
                        raster_poly(pts, nearest((r, g, b)))

        # sprite billboards (R_DrawSprite): explosions, bubbles. Each frame is
        # a screen-aligned rect at the entity origin, scaled by 1/z, drawn
        # texel-by-texel with the depth test; index 255 is transparent.
        if sprites:
            for (sofx, sofy, sw, sh, pix), (spx, spy, spz) in sprites:
                dx, dy, dz = spx - ox, spy - oy, spz - oz
                cz = dx * fx + dy * fy + dz * fz
                if cz < NEAR:
                    continue
                iz = 1.0 / cz
                scx = hw + (dx * rx + dy * ry + dz * rz) * focal * iz
                scy = hh - (dx * ux + dy * uy + dz * uz) * focal * iz
                scale = focal * iz
                x0 = int(scx + sofx * scale)
                y0 = int(scy - sofy * scale)
                wpx = max(1, int(sw * scale))
                hpx = max(1, int(sh * scale))
                for py in range(max(0, y0), min(ih, y0 + hpx)):
                    trow = ((py - y0) * sh // hpx) * sw
                    base = py * iw
                    for px in range(max(0, x0), min(iw, x0 + wpx)):
                        c = pix[trow + (px - x0) * sw // wpx]
                        if c == 255:
                            continue            # transparent texel
                        o = base + px
                        if iz > zb[o]:
                            zb[o] = iz
                            fb[o] = c

        # point-sprite particles (D_DrawParticle, d_part.c): teleport fog,
        # rocket/blood trails, explosions. Each is a small distance-scaled square
        # written straight into the framebuffer with the depth test, so walls
        # occlude it per-pixel -- the flat/wire path can only overlay them with a
        # coarse per-particle line-of-sight check, which is why textured mode
        # showed none. particles: the live [x,y,z, vx,vy,vz, color, die] list.
        if particles:
            pscale = focal * PARTICLE_ZBUF_RADIUS
            iwl = iw; ihl = ih; zbl = zb; fbl = fb
            for p in particles:
                dx = p[0] - ox; dy = p[1] - oy; dz = p[2] - oz
                cz = dx * fx + dy * fy + dz * fz
                if cz < NEAR:
                    continue
                iz = 1.0 / cz
                sx = int(hw + (dx * rx + dy * ry + dz * rz) * focal * iz)
                sy = int(hh - (dx * ux + dy * uy + dz * uz) * focal * iz)
                half = int(pscale * iz)
                if half > PARTICLE_ZBUF_MAX:
                    half = PARTICLE_ZBUF_MAX
                x0 = sx - half; x1 = sx + half + 1
                y0 = sy - half; y1 = sy + half + 1
                if x0 < 0: x0 = 0
                if y0 < 0: y0 = 0
                if x1 > iwl: x1 = iwl
                if y1 > ihl: y1 = ihl
                ci = p[6] & 255
                for py in range(y0, y1):
                    base = py * iwl
                    for o in range(base + x0, base + x1):
                        if iz > zbl[o]:
                            zbl[o] = iz
                            fbl[o] = ci

        # first-person weapon view model: drawn last with a 3x depth bias
        # (VIEWMODEL_ZSCALE, WinQuake's ziscale hack), so it wins the z-test
        # against world geometry it pokes into and its own coaxial barrel
        # triangles stop z-fighting. No PVS cull.
        if view_model:
            mdl, verts, org, ang = view_model
            if textured and mdl.skin_idx is not None:
                raster_alias_tex(mdl, verts, org, ang, VIEWMODEL_ZSCALE)
            else:
                raster_alias(mdl, verts, org, ang, VIEWMODEL_ZSCALE)
        PROFILER.end("raster")

        return (fb, iw, ih), leaf
