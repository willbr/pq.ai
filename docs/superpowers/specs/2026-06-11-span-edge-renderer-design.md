# Span/edge (scanline) world renderer

## Goal

Replace the textured mode's per-pixel z-buffer world fill with a faithful port
of WinQuake's **edge-list / surface-stack / span** renderer (`r_edge.c` +
`d_scan.c`). This is the standing "future work" item in `faults.md`.

The driver is **fidelity to WinQuake**, not speed. We accept that in pure Python
this may be no faster — possibly slower — than the current z-buffer fill. What we
get is the *real* algorithm: occlusion resolved by sorting edges and surfaces,
not by a per-pixel depth compare. The concrete payoff is that the **lift
z-fighting is fixed structurally** (id's surface-stack 1/z tie-break) instead of
by the current depth-bias hack.

Decisions locked during design:

- **Coexistence:** the span/edge renderer *replaces* the textured mode. The
  per-pixel z-buffer world fill (`raster_poly_tex` / `raster_poly_cached`) is
  retired for world & brush geometry. The z-buffer **array** survives, used only
  so alias models and particles drawn afterward occlude against the world.
- **Fidelity level:** algorithm-faithful. Port the pipeline and structures and
  cite `r_edge.c`/`d_scan.c`, but use idiomatic Python float/int math — like the
  rest of `render.py`, which already uses float `1/z` rather than 16.16
  fixed-point. We do **not** replicate fixed-point `u_step` stepping or the
  8/16-pixel affine subdivision (its comment in `raster_poly_cached` already
  found subdivision a wash at sub-10-pixel spans).
- **Lightmap:** faithful blocky surface cache (`D_CacheSurface` port). The span
  fill reads a precombined texture×lightmap cache at blocky 16-texel lightmap
  resolution. This reverts the recent bilinear lightmap smoothing *in this
  path*.

## Reference

Grounded in a full read of `quake-source/WinQuake/`: `r_edge.c` (edge sort →
spans), `d_edge.c` (`D_DrawSurfaces` dispatch), `d_scan.c` (`D_DrawSpans8` /
`D_DrawZSpans` span fill), `r_draw.c` (`R_RenderFace` / `R_EmitEdge` / `R_ClipEdge`),
`d_polyse.c` (`D_PolysetDrawFinalVerts` — alias-model z-test), and the structs in
`r_shared.h` (`edge_t`, `surf_t`, `espan_t`).

Key fact that shapes the design: **WinQuake's z-buffer is written per world
pixel (`D_DrawZSpans`) but never *tested* for the world.** The edge sort resolves
world occlusion; the z-buffer exists so alias models / particles drawn afterward
sort against the world. So this port removes the per-pixel depth *compare* for
world geometry, but still writes `1/z` per world pixel.

## Module boundary

`quake/r_edge.py` is a **pure 2D occlusion engine**: no OS/UI, relative imports,
`python -m quake.r_edge` self-test. It works entirely in screen space and knows
nothing about cameras, textures, or palettes. `render.py` keeps all camera- and
pixel-side work.

The split mirrors `r_edge.c` (edge sort → spans) vs `d_scan.c` (span → pixels):

- **`render.py` — emit (`R_RenderFace` front half):** PVS walk, backface cull,
  transform verts → camera space, near-clip, project to screen, compute each
  face's `(z00, zdx, zdy)` 1/z plane gradients (existing `plane_gradients`), and
  assign a BSP draw-order key.
- **`r_edge.py` — sweep (`R_ScanEdges`):** screen polygons → edges → scanline
  sweep → surface stack → spans.
- **`render.py` — fill (`d_scan.c` role):** walk each surface's spans and fill
  pixels from the surface cache / sky / turb, writing `1/z` to the z-buffer.

### Interface

```python
class EdgeRaster:
    def __init__(self, width, height): ...
    def begin_frame(self): ...                       # reset pools, clear buckets
    def add_surface(self, key, flags, zi_plane, screen_poly) -> Surf
        # zi_plane = (z00, zdx, zdy); screen_poly = [(sx, sy), ...] convex
        # builds the surface's edges into the per-scanline buckets, returns the
        # Surf so render.py can attach its opaque fill handle (surf.fill = ...)
    def scan(self) -> list[Surf]                     # R_ScanEdges; surfs in draw
        # order, each carrying its emitted .spans
```

Usage: `er.begin_frame()`; per face `surf = er.add_surface(...); surf.fill = ...`;
then `for surf in er.scan(): for (u, v, n) in surf.spans: fill(surf, u, v, n)`.

## Data structures (idiomatic Python, mirroring the C structs)

`Surf` (mirrors `surf_t`), one per polygon:

```python
class Surf:
    key          # BSP draw order; stack-sort tie-break field
    flags        # NORMAL / SKY / TURB
    zi           # (z00, zdx, zdy): 1/z plane eq, for the leading-edge tie-break
    fill         # opaque handle render.py uses at fill time
    next, prev   # active-surface-stack links (the list R_LeadingEdge walks)
    spanstate    # 0 = not spanning, 1 = spanning
    last_u       # u where this surface's current span opened
    spans        # list of (u, v, count)
```

Two sentinels: `surfaces[0]` (empty) and `surfaces[1]` (background, always bottom
of stack), seeded as WinQuake does.

`Edge` (mirrors `edge_t`), two per polygon edge:

```python
class Edge:
    u, u_step    # screen x at current scanline, dx/dy step (floats)
    surf_lead    # Surf this edge reveals
    surf_trail   # Surf this edge conceals
    next, prev   # active-edge-list links (kept u-sorted during the sweep)
    nextremove   # removeedges chain
```

Per-scanline buckets `newedges` and `removeedges`: plain Python lists of length
`height`. `newedges[y]` holds edges that activate at row `y` (built u-sorted at
`add_surface` time); `removeedges[y]` holds those retiring after row `y`.

Span output: `(u, v, count)` tuples appended to `surf.spans` — the `espan_t`
chain flattened.

Allocation: `Edge`/`Surf` are allocated per frame. Fidelity > speed, so that is
acceptable. If GC churn bites, pool and reset in `begin_frame` (grow-once, like
the renderer's existing line/poly pools). Pooling is noted as an available
optimization, not built up front (YAGNI).

## Per-frame data flow

1. **Begin.** `er.begin_frame()` — reset edge/surface pools, clear the
   `newedges`/`removeedges` buckets, seed `surfaces[1]` (background).

2. **Emit (render.py).** PVS-walk visible faces (existing), plus each brush-model
   entity face (doors/lifts), in BSP order. Per face: backface cull, transform →
   camera, near-clip, project to screen, compute `(z00, zdx, zdy)`. Assign a
   monotonic `key`; brush-model faces get the entity's own key so coplanar
   lift-vs-world surfaces collide on key and fall to the 1/z tie-break.
   `surf = er.add_surface(key, flags, zi, screen_poly)`; attach `surf.fill`.

3. **Scan (r_edge.py, `R_ScanEdges`).** Per scanline `y`, top→bottom:
   - merge `newedges[y]` into the active edge list (`R_InsertNewEdges`,
     u-sorted);
   - sweep active edges left→right. A **leading** edge pushes its surface:
     `R_LeadingEdge` finds the stack slot by `key`, and on a key tie does the
     1/z compare at the edge's `u` with id's ±1% fudge. A **trailing** edge pops
     (`R_TrailingEdge`). Whenever the top-of-stack surface changes, close the old
     top's span (`last_u..u`) and open the new top's — appending `(u, v, count)`
     to the winning surface's `.spans`.
   - retire `removeedges[y]`, then step every active edge `u += u_step`.

4. **Fill (render.py, `d_scan.c` role).** `for surf in er.scan(): for (u,v,n) in
   surf.spans:` fill that run — NORMAL from the blocky surface cache, SKY/TURB
   via their warp fills — writing color to `fb` **and** `1/z` to `zb`, **no depth
   test** (occlusion already resolved).

5. **Models & particles (unchanged).** After the world is laid down, alias models
   and particles draw via the existing z-buffered paths (**test and write** `zb`),
   so they occlude against the world the spans z-filled. Then present (existing).

The inversion: occlusion moves *out* of the pixel loop (step 3, once per span)
and the pixel loop (step 4) becomes an unconditional fill. The z-buffer survives
only for step 5.

## Surface cache, sky, and turbulent fills

- **Surface cache (`D_CacheSurface` port).** Precombine each surface's
  texture×lightmap into a palette-index cache spanning the face's s/t extent, at
  blocky 16-texel lightmap resolution (nearest luxel, not bilinear). The span
  fill then samples one cache texel per pixel (perspective-correct `u,v`
  recovered from `1/z`), with no per-pixel lightmap lookup. The current
  `_surface_cache`/`raster_poly_cached` infrastructure is adapted: switch its
  luxel sampling from bilinear back to blocky, and feed it from spans instead of
  polygon scanlines. Cache entries are keyed by surface and rebuilt when the
  lightmap animates (light styles) — same trigger as today.
- **Sky.** Surfaces flagged SKY fill with the existing two-layer scroll
  (`R_InitSky` foreground/background drift) — same fill code, now driven per span.
- **Turbulent (water/lava/slime/teleport).** Surfaces flagged TURB fill with the
  existing sine-warp (`_TURBSIN`) — again the same fill, driven per span.

All three write `1/z` to `zb` so models sort against them.

## Z-buffer role after the change

The `zb` array stays. World spans **write** `1/z` (no test). Alias models and
particles **test and write** `zb` as they do today. The only behavioral change is
that world/brush pixels are now laid down write-only from spans rather than
test-and-write per polygon. The bmodel depth-bias hack (`render.py` ~line 222)
is removed; the surface-stack tie-break replaces it.

## Testing

- **`test_r_edge.py` (new, pure 2D — no pak):** synthetic scenes exercising the
  sweep in isolation:
  - two overlapping screen rectangles at different depths → the nearer
    surface's spans cover the overlap, the farther's are clipped to the
    remainder (zero overlap, zero gap);
  - three stacked surfaces → correct leading/trailing push/pop order;
  - two coplanar surfaces with equal keys → the 1/z fudge tie-break picks the
    nearer deterministically (the lift case, abstracted);
  - a surface fully behind another → no spans emitted;
  - edges clamped to the viewport; degenerate sliver polygons emit nothing.
- **`python -m quake.r_edge`** module self-test (smoke).
- **`test_lift_zfight.py` (existing):** should now pass via the structural fix.
  Adapt it to assert no flicker without relying on the depth bias.
- **`test_zbuffer_raster.py` goldens:** textured mode output changes (span/edge +
  blocky surface cache). Regenerate with `--regen`, eyeball for correctness,
  commit the new goldens.
- **Full-stack boot:** render a frame of e1m1 through the new path, assert no
  crash and sane span counts. Use the existing `_boot()` helper pattern.

Run muted: `PQ_AUDIO=0` (CoreAudio segfaults headless).

## Risks / notes

- **Performance.** Likely slower; acceptable per the goal. `ZBUF_SCALE` stays so
  it remains interactive. Profiler (`P` HUD) will quantify the change.
- **Surface-stack churn** (linked-list insert/remove per scanline) and per-edge
  `u` stepping are the Python-expensive parts; pooling `Edge`/`Surf` is the
  escape hatch if needed.
- **Correctness of the key ordering.** The BSP draw-order key must be monotonic
  for the stack sort to be correct; brush-model keys must collide with the world
  at coplanar surfaces so the fudge engages. This is the subtle part — covered by
  the `test_r_edge.py` tie-break test and the lift test.
- **Sky/turb/animated textures** keep their existing fills; only the *iteration*
  (spans vs polygon scanlines) changes, limiting blast radius.
