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
from array import array

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
# sky drift: texels/sec the scrolling sky texture slides across its faces.
SKY_SCROLL = 24.0

# lightmap luxels are 0..255; Quake brightens them with overbright bits we don't
# emulate, so a gain keeps lit areas from looking muddy. DEFAULT_LIGHT is what an
# unlit surface / off-map alias model gets so it stays visible.
LIGHT_GAIN = 1.6
DEFAULT_LIGHT = 180
# flat-shaded mode has no texture detail, so it leans on the texture average
# being brightened more (it used a 2.2 directional gain before lighting existed).
FLAT_LIGHT_GAIN = 2.2 / 255.0


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


def angle_vectors(yaw_deg, pitch_deg):
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    forward = (cp * cy, cp * sy, -sp)
    right = (sy, -cy, 0.0)
    up = (sp * cy, sp * sy, cp)
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


def _bsp_texture_rgb(bsp, palette):
    """Decode each miptex of `bsp` to (w, h, packed_rgb), or None where unusable.
    Standalone twin of Renderer._decode_textures, for texturing pickup models."""
    out = []
    for t in bsp.textures:
        if t is None or t[3] is None or palette is None:
            out.append(None)
            continue
        _name, w, h, idx = t
        if w <= 0 or h <= 0 or len(idx) < w * h:
            out.append(None)
            continue
        rgb = bytearray(w * h * 3)
        o = 0
        for px in idx:
            pr, pg, pb = palette[px]
            rgb[o] = pr; rgb[o + 1] = pg; rgb[o + 2] = pb
            o += 3
        out.append((w, h, rgb))
    return out


class PickupModel:
    """An external .bsp brush model loaded as a pickup (health box, ammo box).

    These are standalone little Quake BSPs (maps/b_bh25.bsp, maps/b_shell0.bsp,
    ...), not inline '*N' submodels of the world and not .mdl alias models, so
    neither the brush-model nor the alias path drew them. Each is a convex box,
    so we precompute its faces in model-local space and draw it with backface
    culling alone -- no internal BSP walk or face sorting needed.

    self.faces: list of (local_verts, (nx, ny, nz, dist), color_hex, (r,g,b),
                         texrec, s_vec, t_vec) where texrec is (w,h,rgb) or None
                         and s_vec/t_vec map a local vertex to texel coords.
    self.mins/self.maxs: model-space bounds, for PVS + painter routing.
    """
    def __init__(self, bsp, palette):
        m0 = bsp.models[0]
        self.mins = tuple(m0["mins"])
        self.maxs = tuple(m0["maxs"])
        tex_rgb = _bsp_texture_colors(bsp, palette)     # average colour (flat mode)
        tex_full = _bsp_texture_rgb(bsp, palette)       # full texels (textured mode)
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
    def __init__(self, bsp, palette=None):
        self.bsp = bsp
        self.palette = palette          # list of 256 (r,g,b) for texture colours
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
        self.face_color_rgb = []        # (r,g,b) ints, for the z-buffer flat fill
        self.face_base_rgb = []         # raw texture average, lit per-face by flat mode
        for fi, (nx, ny, nz, dist) in enumerate(self.face_plane):
            inten = (0.50 + 0.50 * max(0.0, nx * lx + ny * ly + nz * lz)) * gain
            base = None
            ti = bsp.faces[fi][4]
            if 0 <= ti < len(texinfo):
                mt = texinfo[ti][0]
                if 0 <= mt < len(tex_rgb):
                    base = tex_rgb[mt]
            if base is None:
                base = (140.0, 140.0, 140.0)
            self.face_base_rgb.append(base)
            r = min(255, int(base[0] * inten))
            g = min(255, int(base[1] * inten))
            b = min(255, int(base[2] * inten))
            self.face_color_rgb.append((r, g, b))

        # full-resolution textures decoded to packed RGB bytes (once), for the
        # textured z-buffer rasteriser. Aligned to bsp.textures; None where the
        # miptex has no level-0 pixels or no palette was supplied.
        self.tex_rgb = self._decode_textures()

        # classify miptexes by Quake's name conventions: sky* scroll, *liquids
        # warp (water/lava/slime/teleport), and +N frames cycle. _classify_tex
        # also builds the animation chains (per-miptex list of frame indices).
        self.is_sky, self.is_turb, self.tex_anim = self._classify_textures()

        # per-face texture record for the rasteriser: (w, h, rgb_bytes, s_vec,
        # t_vec) or None to fall back to flat colour. Plus a per-face integer
        # shade (0..256, 8-bit fixed point) from the same directional light, so
        # textured surfaces still read their facing -- textures are full-bright
        # palette colours, so no 2.2 gain here (that only rescued dark averages).
        self.face_tex = []
        self.face_shade = []
        self.face_sky = [False] * nfaces      # scroll the sky texture
        self.face_turb = [False] * nfaces     # sine-warp (liquids, teleporters)
        self.face_anim = [None] * nfaces      # [(w,h,rgb), ...] frames, or None
        for fi, (nx, ny, nz, dist) in enumerate(self.face_plane):
            self.face_shade.append(int((0.55 + 0.45 * max(0.0,
                                   nx * lx + ny * ly + nz * lz)) * 256))
            rec = None
            ti = bsp.faces[fi][4]
            if 0 <= ti < len(texinfo):
                mt = texinfo[ti][0]
                if 0 <= mt < len(self.tex_rgb):
                    self.face_sky[fi] = self.is_sky[mt]
                    self.face_turb[fi] = self.is_turb[mt]
                    if self.tex_anim[mt] is not None:
                        frames = [self.tex_rgb[m] for m in self.tex_anim[mt]
                                  if self.tex_rgb[m] is not None]
                        if len(frames) > 1:
                            self.face_anim[fi] = frames
                    if self.tex_rgb[mt] is not None:
                        w, h, rgb = self.tex_rgb[mt]
                        rec = (w, h, rgb, texinfo[ti][2], texinfo[ti][3])
            self.face_tex.append(rec)
        # faces whose texture cycles -- the only ones _animate_surfaces touches
        self.anim_faces = [fi for fi in range(nfaces)
                           if self.face_anim[fi] is not None]

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

    def _texture_colors(self):
        """Average RGB per miptex via a palette histogram. None where unusable."""
        from collections import Counter
        pal = self.palette
        out = []
        for t in self.bsp.textures:
            if t is None or t[3] is None or pal is None:
                out.append(None)
                continue
            r = g = b = tot = 0
            for idx, c in Counter(t[3]).items():     # idx -> pixel count
                pr, pg, pb = pal[idx]
                r += pr * c
                g += pg * c
                b += pb * c
                tot += c
            out.append((r / tot, g / tot, b / tot) if tot else None)
        return out

    def _decode_textures(self):
        """Decode each miptex's level-0 palette indices to packed RGB bytes,
        once. Returns a list aligned to bsp.textures: (w, h, rgb_bytearray) or
        None where unusable. The rasteriser samples these directly."""
        pal = self.palette
        out = []
        for t in self.bsp.textures:
            if t is None or t[3] is None or pal is None:
                out.append(None)
                continue
            name, w, h, idx = t
            if w <= 0 or h <= 0 or len(idx) < w * h:
                out.append(None)
                continue
            rgb = bytearray(w * h * 3)
            o = 0
            for px in idx:
                pr, pg, pb = pal[px]
                rgb[o] = pr; rgb[o + 1] = pg; rgb[o + 2] = pb
                o += 3
            out.append((w, h, rgb))
        return out

    def _classify_textures(self):
        """Split miptexes by Quake's name conventions and build +N animation
        chains. Returns (is_sky, is_turb, tex_anim) aligned to bsp.textures:
          - is_sky[mt]:  name starts 'sky'  -> scrolling sky.
          - is_turb[mt]: name starts '*'    -> sine-warped liquid/teleporter.
          - tex_anim[mt]: the main-sequence frame list ['+0x','+1x',...] this
            miptex belongs to (sorted by digit), or None. The alternate '+a..'
            sequence (entity-triggered) is ignored -- world surfaces only cycle
            the main one."""
        textures = self.bsp.textures
        n = len(textures)
        is_sky = [False] * n
        is_turb = [False] * n
        tex_anim = [None] * n
        groups = {}                  # base name -> {digit: miptex index}
        for mt, t in enumerate(textures):
            if t is None:
                continue
            name = t[0].lower()
            if name.startswith("sky"):
                is_sky[mt] = True
            elif name.startswith("*"):
                is_turb[mt] = True
            elif name.startswith("+") and len(name) >= 2 and name[1].isdigit():
                groups.setdefault(name[2:], {})[int(name[1])] = mt
        for frames in groups.values():
            chain = [frames[k] for k in sorted(frames)]
            for mt in chain:
                tex_anim[mt] = chain
        return is_sky, is_turb, tex_anim

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
        frame; _zb_zero seeds the depth buffer to 0 (= infinitely far, since we
        store 1/z and keep the larger value)."""
        if self.video_res is not None:
            self.zw, self.zh = self.video_res        # fixed mode (video menu)
        else:
            self.zw = max(1, self.width // self.zbuf_scale)
            self.zh = max(1, self.height // self.zbuf_scale)
        self._bg_frame = bytes(ZBUF_BG) * (self.zw * self.zh)
        self._zb_zero = bytes(4 * self.zw * self.zh)

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
               view_model=None, bsp_ents=None):
        bsp = self.bsp
        self.frame += 1
        frame = self.frame
        forward, right, up = angle_vectors(yaw, pitch)
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
                brush_ents = [(i, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
                              for i in range(1, len(bsp.models))]
            for mi, (ofx, ofy, ofz), _ang in brush_ents:
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
                      view_model=None, bsp_ents=None, lightstyles=None):
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
        forward, right, up = angle_vectors(yaw, pitch)
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
                brush_ents = [(i, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
                              for i in range(1, len(bsp.models))]
            for mi, ofs, _ang in brush_ents:
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
                       lightstyles=None, time=0.0):
        """True per-pixel z-buffered software rasteriser. World/brush faces are
        perspective-correct texture-mapped (textured=True) or flat-shaded; both
        resolve occlusion with a 1/z depth buffer (no painter's ordering, so
        intersecting geometry no longer mis-sorts). Alias models and pickups stay
        flat-shaded. Returns ((framebuffer, w, h), leaf); the buffer is raw RGB
        bytes the UI wraps in a PPM image and scales up."""
        bsp = self.bsp
        self.frame += 1
        frame = self.frame
        forward, right, up = angle_vectors(yaw, pitch)
        ox, oy, oz = origin
        fx, fy, fz = forward
        rx, ry, rz = right
        ux, uy, uz = up

        vertexes = bsp.vertexes
        leafs = bsp.leafs
        marks = bsp.marksurfaces
        face_verts = self.face_verts
        face_plane = self.face_plane
        face_color_rgb = self.face_color_rgb
        face_tex = self.face_tex
        face_lm = self.face_lm
        face_sky = self.face_sky
        face_turb = self.face_turb
        sky_off = (time * SKY_SCROLL) % 256.0     # sky texels scrolled this frame
        face_frame = self.face_frame
        vert_frame = self.vert_frame
        vcache = self.vcache

        iw, ih = self.zw, self.zh
        focal = self.focal * iw / self.width          # focal scaled to the small fb
        hw = iw * 0.5
        hh = ih * 0.5
        fb = bytearray(self._bg_frame)                # fresh background to draw over
        zb = array('f', self._zb_zero)                # depth = 1/z, 0 == far away

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

        def raster_tri(ax, ay, az, bx, by, bz, cx, cy, cz, r, g, b):
            # a,b,c are screen-space (x, y, invz). Edge-function fill over the
            # triangle's pixel bounding box, clamped to the framebuffer; depth is
            # the perspective-correct 1/z interpolated from the vertices.
            x0 = ax if ax < bx else bx
            if cx < x0: x0 = cx
            x1 = ax if ax > bx else bx
            if cx > x1: x1 = cx
            y0 = ay if ay < by else by
            if cy < y0: y0 = cy
            y1 = ay if ay > by else by
            if cy > y1: y1 = cy
            x0 = int(x0)
            if x0 < 0: x0 = 0
            x1 = int(x1) + 1
            if x1 > iw: x1 = iw
            y0 = int(y0)
            if y0 < 0: y0 = 0
            y1 = int(y1) + 1
            if y1 > ih: y1 = ih
            if x0 >= x1 or y0 >= y1:
                return
            area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if area == 0.0:
                return
            # barycentric edge weights (orient2d): w_i / area is vertex i's weight,
            # stepping A in +x and B in +y. Sign convention must match `area`.
            A0 = by - cy; B0 = cx - bx          # weight for vertex a (edge b->c)
            A1 = cy - ay; B1 = ax - cx          # weight for vertex b (edge c->a)
            A2 = ay - by; B2 = bx - ax          # weight for vertex c (edge a->b)
            px = x0 + 0.5; py = y0 + 0.5
            w0r = (cx - bx) * (py - by) - (cy - by) * (px - bx)
            w1r = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
            w2r = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            if area < 0.0:                       # normalise winding -> test w >= 0
                A0 = -A0; A1 = -A1; A2 = -A2
                B0 = -B0; B1 = -B1; B2 = -B2
                w0r = -w0r; w1r = -w1r; w2r = -w2r
                area = -area
            inv = 1.0 / area                     # fold into depths: izp = sum(w*z)
            za = az * inv; zbb = bz * inv; zc = cz * inv
            for y in range(y0, y1):
                w0 = w0r; w1 = w1r; w2 = w2r
                row = y * iw
                for x in range(x0, x1):
                    if w0 >= 0.0 and w1 >= 0.0 and w2 >= 0.0:
                        iz = w0 * za + w1 * zbb + w2 * zc
                        idx = row + x
                        if iz > zb[idx]:
                            zb[idx] = iz
                            o = idx * 3
                            fb[o] = r; fb[o + 1] = g; fb[o + 2] = b
                    w0 += A0; w1 += A1; w2 += A2
                w0r += B0; w1r += B1; w2r += B2

        def raster_poly(cam, r, g, b):
            # near-plane clip (z >= NEAR), project to the small fb, fan-triangulate
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
            n = len(out)
            if n < 3:
                return
            sx = []; sy = []; sz = []
            for vx, vy, vz in out:
                iz = 1.0 / vz
                sx.append(hw + vx * focal * iz)
                sy.append(hh - vy * focal * iz)
                sz.append(iz)
            for k in range(1, n - 1):
                raster_tri(sx[0], sy[0], sz[0], sx[k], sy[k], sz[k],
                           sx[k + 1], sy[k + 1], sz[k + 1], r, g, b)

        def raster_tri_tex(ax, ay, az, au, av, bx, by, bz, bu, bv,
                           cx, cy, cz, cu, cv, tw, th, tex,
                           lmw, lmh, lsmin, ltmin, lux):
            # textured z-buffered triangle. Vertices carry (x, y, invz, u*invz,
            # v*invz); invz/u*invz/v*invz interpolate linearly in screen space,
            # so per pixel u,v = (interp)/invz recovers perspective-correct texels.
            # The texel is then modulated by the lightmap luxel covering it.
            x0 = ax if ax < bx else bx
            if cx < x0: x0 = cx
            x1 = ax if ax > bx else bx
            if cx > x1: x1 = cx
            y0 = ay if ay < by else by
            if cy < y0: y0 = cy
            y1 = ay if ay > by else by
            if cy > y1: y1 = cy
            x0 = int(x0)
            if x0 < 0: x0 = 0
            x1 = int(x1) + 1
            if x1 > iw: x1 = iw
            y0 = int(y0)
            if y0 < 0: y0 = 0
            y1 = int(y1) + 1
            if y1 > ih: y1 = ih
            if x0 >= x1 or y0 >= y1:
                return
            area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if area == 0.0:
                return
            A0 = by - cy; B0 = cx - bx
            A1 = cy - ay; B1 = ax - cx
            A2 = ay - by; B2 = bx - ax
            px = x0 + 0.5; py = y0 + 0.5
            w0r = (cx - bx) * (py - by) - (cy - by) * (px - bx)
            w1r = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
            w2r = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            if area < 0.0:
                A0 = -A0; A1 = -A1; A2 = -A2
                B0 = -B0; B1 = -B1; B2 = -B2
                w0r = -w0r; w1r = -w1r; w2r = -w2r
                area = -area
            inv = 1.0 / area
            za = az * inv; zbb = bz * inv; zc = cz * inv
            ua = au * inv; ub = bu * inv; uc = cu * inv
            va = av * inv; vb = bv * inv; vc = cv * inv
            for y in range(y0, y1):
                w0 = w0r; w1 = w1r; w2 = w2r
                row = y * iw
                for x in range(x0, x1):
                    if w0 >= 0.0 and w1 >= 0.0 and w2 >= 0.0:
                        iz = w0 * za + w1 * zbb + w2 * zc
                        idx = row + x
                        if iz > zb[idx]:
                            z = 1.0 / iz
                            u = (w0 * ua + w1 * ub + w2 * uc) * z
                            v = (w0 * va + w1 * vb + w2 * vc) * z
                            lc = int((u - lsmin) * 0.0625)
                            if lc < 0: lc = 0
                            elif lc >= lmw: lc = lmw - 1
                            lr = int((v - ltmin) * 0.0625)
                            if lr < 0: lr = 0
                            elif lr >= lmh: lr = lmh - 1
                            sh = lux[lr * lmw + lc]      # lightmap luxel 0..255
                            o = (int(v) % th * tw + int(u) % tw) * 3
                            zb[idx] = iz
                            fo = idx * 3
                            fb[fo] = (tex[o] * sh) >> 8
                            fb[fo + 1] = (tex[o + 1] * sh) >> 8
                            fb[fo + 2] = (tex[o + 2] * sh) >> 8
                    w0 += A0; w1 += A1; w2 += A2
                w0r += B0; w1r += B1; w2r += B2

        def raster_poly_tex(cam, rec, lm):
            # cam: list of (cx, cy, cz, u, v). Near-clip (z >= NEAR) interpolating
            # u,v too, project, fan-triangulate into textured triangles. lm is the
            # face's lightmap (lmw, lmh, smin, tmin, luxels) sampled per pixel.
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
            n = len(out)
            if n < 3:
                return
            sx = []; sy = []; sz = []; su = []; sv = []
            for cx, cy, cz, u, v in out:
                iz = 1.0 / cz
                sx.append(hw + cx * focal * iz)
                sy.append(hh - cy * focal * iz)
                sz.append(iz)
                su.append(u * iz)              # u/z, linear in screen space
                sv.append(v * iz)
            for k in range(1, n - 1):
                raster_tri_tex(sx[0], sy[0], sz[0], su[0], sv[0],
                               sx[k], sy[k], sz[k], su[k], sv[k],
                               sx[k + 1], sy[k + 1], sz[k + 1], su[k + 1], sv[k + 1],
                               tw, th, tex, lmw, lmh, lsmin, ltmin, lux)

        def raster_tri_tex_turb(ax, ay, az, au, av, bx, by, bz, bu, bv,
                                cx, cy, cz, cu, cv, tw, th, tex):
            # Turbulent (liquid/teleport) triangle: same perspective-correct
            # setup as raster_tri_tex, but each pixel's texel is sine-warped in
            # both axes and drawn full-bright (no lightmap -- these are special
            # surfaces). Hot but only over liquid/sky area, so kept separate
            # from the common path rather than branching per pixel there.
            sintab = _TURBSIN; scale = _TURBSCALE; tt = time
            x0 = ax if ax < bx else bx
            if cx < x0: x0 = cx
            x1 = ax if ax > bx else bx
            if cx > x1: x1 = cx
            y0 = ay if ay < by else by
            if cy < y0: y0 = cy
            y1 = ay if ay > by else by
            if cy > y1: y1 = cy
            x0 = int(x0)
            if x0 < 0: x0 = 0
            x1 = int(x1) + 1
            if x1 > iw: x1 = iw
            y0 = int(y0)
            if y0 < 0: y0 = 0
            y1 = int(y1) + 1
            if y1 > ih: y1 = ih
            if x0 >= x1 or y0 >= y1:
                return
            area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if area == 0.0:
                return
            A0 = by - cy; B0 = cx - bx
            A1 = cy - ay; B1 = ax - cx
            A2 = ay - by; B2 = bx - ax
            px = x0 + 0.5; py = y0 + 0.5
            w0r = (cx - bx) * (py - by) - (cy - by) * (px - bx)
            w1r = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
            w2r = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            if area < 0.0:
                A0 = -A0; A1 = -A1; A2 = -A2
                B0 = -B0; B1 = -B1; B2 = -B2
                w0r = -w0r; w1r = -w1r; w2r = -w2r
                area = -area
            inv = 1.0 / area
            za = az * inv; zbb = bz * inv; zc = cz * inv
            ua = au * inv; ub = bu * inv; uc = cu * inv
            va = av * inv; vb = bv * inv; vc = cv * inv
            for y in range(y0, y1):
                w0 = w0r; w1 = w1r; w2 = w2r
                row = y * iw
                for x in range(x0, x1):
                    if w0 >= 0.0 and w1 >= 0.0 and w2 >= 0.0:
                        iz = w0 * za + w1 * zbb + w2 * zc
                        idx = row + x
                        if iz > zb[idx]:
                            z = 1.0 / iz
                            u = (w0 * ua + w1 * ub + w2 * uc) * z
                            v = (w0 * va + w1 * vb + w2 * vc) * z
                            su2 = u + sintab[int((v * 0.125 + tt) * scale) & 255]
                            sv2 = v + sintab[int((u * 0.125 + tt) * scale) & 255]
                            o = (int(sv2) % th * tw + int(su2) % tw) * 3
                            zb[idx] = iz
                            fo = idx * 3
                            fb[fo] = tex[o]
                            fb[fo + 1] = tex[o + 1]
                            fb[fo + 2] = tex[o + 2]
                    w0 += A0; w1 += A1; w2 += A2
                w0r += B0; w1r += B1; w2r += B2

        def raster_poly_tex_turb(cam, rec):
            # near-clip + project + fan, like raster_poly_tex, into warped tris
            tw, th, tex = rec[0], rec[1], rec[2]
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
            n = len(out)
            if n < 3:
                return
            sx = []; sy = []; sz = []; su = []; sv = []
            for cx, cy, cz, u, v in out:
                iz = 1.0 / cz
                sx.append(hw + cx * focal * iz)
                sy.append(hh - cy * focal * iz)
                sz.append(iz)
                su.append(u * iz)
                sv.append(v * iz)
            for k in range(1, n - 1):
                raster_tri_tex_turb(sx[0], sy[0], sz[0], su[0], sv[0],
                                    sx[k], sy[k], sz[k], su[k], sv[k],
                                    sx[k + 1], sy[k + 1], sz[k + 1],
                                    su[k + 1], sv[k + 1], tw, th, tex)

        def emit_face(fi, pts, rec):
            # dispatch a world/brush face to the right sampler: warped liquid,
            # scrolled sky, or the plain lightmapped path.
            if face_turb[fi]:
                raster_poly_tex_turb(pts, rec)
            elif face_sky[fi]:
                raster_poly_tex([(p[0], p[1], p[2], p[3] + sky_off, p[4])
                                 for p in pts], rec, face_lm[fi])
            else:
                raster_poly_tex(pts, rec, face_lm[fi])

        def raster_alias(mdl, verts, org, ang):
            # rotate model verts into world, transform to camera, flat-shade each
            # triangle by its world normal (matches render_shaded's emit_alias).
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
                raster_poly([cam[a], cam[b], cam[c]], r, g, bl)

        def raster_alias_tex(mdl, verts, org, ang):
            # textured alias model: skin-mapped per triangle, lit by the baked
            # light sampled at the model's origin (so a monster in a dark room is
            # dark) modulated by each triangle's facing.
            rec = mdl.skin_rgb                       # (skinw, skinh, rgb_bytes)
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
                                 (cc[0], cc[1], cc[2], s2, t2)], rec, lm)

        PROFILER.begin("raster")        # per-pixel fill of all visible geometry
        # world (model 0): PVS-visible leaves' faces, backface-culled
        for li in visible_leaves:
            _, _, firstmark, nummark = leafs[li]
            for m in range(firstmark, firstmark + nummark):
                fi = marks[m]
                if face_frame[fi] == frame:
                    continue
                face_frame[fi] = frame
                nx, ny, nz, dist = face_plane[fi]
                if ox * nx + oy * ny + oz * nz - dist <= BACKFACE_EPS:
                    continue
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
                    r, g, b = face_color_rgb[fi]
                    raster_poly([transform(vi) for vi in face_verts[fi]], r, g, b)

        # brush-model entities (doors, lifts, buttons), each offset to its origin
        if self.brushmodels:
            if brush_ents is None:
                brush_ents = [(i, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
                              for i in range(1, len(bsp.models))]
            for mi, (ofx, ofy, ofz), _ang in brush_ents:
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
                    rec = face_tex[fi] if textured else None
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
                        r, g, b = face_color_rgb[fi]
                        raster_poly(pts, r, g, b)

        # alias (.mdl) entities -- monsters, items
        if alias_ents:
            for mdl, verts, org, ang in alias_ents:
                r = mdl.boundingradius
                if not self.box_in_pvs((org[0] - r, org[1] - r, org[2] - r),
                                       (org[0] + r, org[1] + r, org[2] + r), vis):
                    continue
                if textured and mdl.skin_rgb is not None:
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
                        raster_poly(pts, r, g, b)

        # first-person weapon view model: drawn last; sits at the camera so its
        # near depth wins the z-test and it reads as on top. No PVS cull.
        if view_model:
            mdl, verts, org, ang = view_model
            if textured and mdl.skin_rgb is not None:
                raster_alias_tex(mdl, verts, org, ang)
            else:
                raster_alias(mdl, verts, org, ang)
        PROFILER.end("raster")

        return (fb, iw, ih), leaf
