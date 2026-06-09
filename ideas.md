things to fix in texture view
* button textures dont change when you press them
* sky texture is double layered

review if we're using a palleted rendering like quake1
review what's cached
review D.CacheSurface

---

## Texture-view bug review (root causes)

Reviewed against id's WinQuake source and the real e1m1 data. e1m1 has the
relevant assets: button textures `+0basebtn / +1basebtn / +abasebtn` and one
sky `sky4` (256x128). All three findings are code-analysis (no live window run).

### 1. Button textures don't change when pressed — CONFIRMED, two missing pieces

Quake's rule (`buttons.qc`): pressing sets `self.frame = 1` ("use alternate
textures"), releasing sets it back to 0. The renderer (`R_TextureAnimation`,
r_surf.c) reads `currententity->frame`: if non-zero it swaps the surface's
base texture to `base->alternate_anims` — the `+a..+j` chain — before drawing.
So `+0basebtn` (idle) must become `+abasebtn` (lit) when frame==1.

We do neither half:
- **The frame never reaches the renderer.** `sv.brush_models()` (sv.py:715)
  returns only `(submodel_index, origin, angles)` — no `frame` field. The
  textured brush-entity loop (render.py:1964) likewise ignores frame and draws
  every submodel face with the *static* world `face_tex[fi]`.
- **The alternate chain is never built.** `_classify_textures` (render.py:545)
  deliberately keeps only the main `+0..+9` sequence and drops `+a..+j`
  ("world surfaces only cycle the main one"). So `+abasebtn` is loaded as a
  texture but has no link from `+0basebtn`, and `_animate_surfaces` only cycles
  the main chain by time. There is no entity-frame → alternate-texture path at
  all.

Fix sketch (small, self-contained):
  1. `brush_models()`: also return the edict's `frame` (it's a known field).
  2. `_classify_textures`: also build base→alternate map (group `+a..+j` by the
     same `name[2:]` key, à la model.c's anims/altanims; expose
     `self.tex_alt[mt] = alt_mt`).
  3. In the brush-entity emit loop, when `frame` is set and the face's miptex has
     an alternate, substitute the alternate texture's `rec` for those faces
     before `emit_face`. The surface cache already invalidates on texture
     identity change (the `ent[3] is tex` check at render.py:712), and each
     submodel owns a disjoint face range, so per-face caching stays correct —
     no cache-key change needed.

### 2. Sky is "double layered" — CONFIRMED, format not split into its two layers

A Quake sky miptex is 256x128 = two stacked 128x128 layers (`R_InitSky`,
r_sky.c): the **left** 128 is the foreground cloud layer (palette index 0 =
transparent), the **right** 128 is the solid background. Quake composites them
each frame (`R_MakeSky`) — background and masked foreground scrolled at
*different* speeds — into a single 128-wide tile, then samples that with a
0x7F (128) mask.

We treat `sky4` as one ordinary 256-wide tiled texture: `emit_face`
(render.py:1808) just adds `sky_off` to the s coord and calls `raster_poly_tex`
with the full `rec` (w=256), which wraps with `% 256`. Result: both 128-px
halves tile side-by-side and repeat across the sky faces — i.e. you see the
cloud layer *and* the background layer laid next to each other = "double
layered." It also ignores the index-0 transparency and the two-speed parallax.

Fix sketch: at load, split sky miptexes into fg (left, 0=transparent) + bg
(right) 128x128 layers. For sky faces, sample bg at one scroll speed and overlay
fg (skipping index 0) at a faster speed, masked to 128. Cheapest correct
version: precompute a composited 128-wide tile per frame like `R_MakeSky` and
feed that as the sky `rec` (so the hot raster loop is unchanged); or do the
two-tap composite inside a dedicated sky sampler. Either kills the doubling.

### 3. Palettized rendering / caching / D_CacheSurface — already faithful

- **Palettized like Quake 1: yes.** This is the recent work (commit 400e2bd).
  `tex_idx` keeps raw 8-bit palette indices; the framebuffer is 8-bit
  palette-indexed and lit by mapping each texel through a `colormap.lmp` row
  (`_cmap_rows`) via `bytes.translate` — exactly Quake's software path. No RGB
  expansion in the inner loop.
- **`_surface_cache` (render.py:697) is a faithful `D_CacheSurface` port:** the
  texture tiled over the face's full s/t extent, each 16-texel luxel cell lit in
  one `bytes.translate` through its colormap row, so the rasteriser does one
  fetch per pixel with no per-pixel lightmap/shade math. Invalidated when the
  lightmap recombines (`_combine_face` drops the entry) or the texture swaps
  (`is` identity check). Bounded at 64 MB with a crude full-flush.
- **Other caches:** `_idx_cache` (nearest-palette memo for runtime RGB),
  per-vertex transform cache (`vcache`, staleness-marked per frame),
  `face_lit_hex` (lit flat-shade colour), and lightmap luxels recombined only
  when a light style's brightness changes. No correctness concerns found here;
  the design already supports the texture swaps that fixes #1 and #2 need.

### Notes on fixing
Both #1 and #2 are isolated to render.py (+ one extra field from
`sv.brush_models`). Neither touches the QC VM or physics. The surface cache's
texture-identity invalidation means the button fix needs no cache changes.

# Ideas / backlog

Ordered by recommended sequence. Rationale: measure before optimizing; make tuning
interactive; then chase the wins; gate the expensive, uncertain work behind data.

1. **Profiler** — do first; everything else depends on it. A lightweight per-frame
   section timer (server frame / cull+transform / rasterise / Tk blit) shown as an
   on-screen HUD, not `cProfile` (its per-call overhead distorts the per-pixel loops).
   Keep `py-spy` as a dev-only sampling tool; don't add it as a dependency.

2. **Console** — DONE. Quake-style console + command/cvar table (`quake/console.py`,
   F1 to toggle, gdi32 frontend). Toggles the render modes, live `zbuf_scale` cvar,
   noclip, map changes, surfaces the profiler HUD; god/give cheats; stdout capture.

3. **Dynamic resolution, target 60fps** — textured z-buffer mode only (it already renders
   at `1/ZBUF_SCALE`). Closed loop: measure frametime → nudge scale toward a ~16.7ms
   budget with hysteresis + clamped range. Pre-allocate one framebuffer/PhotoImage set per
   scale level (don't reallocate in the hot path). Does NOT help wireframe (Tk-bound);
   its lever is segment/detail culling.

4. **Minimize garbage collection** — continuous companion to 1 & 3. Kill per-frame
   allocations in hot loops (temp tuples/lists/arrays), reuse preallocated
   `array`/`bytearray` buffers, consider `gc.disable()` + manual `gc.collect()` at level
   load or raised thresholds. Concrete target: `Mixer.mix()` allocates `out = [0]*N` and a
   fresh `array('h')` every callback. Builds on the existing Canvas line/poly pools.

5. **Multi-threading or processes** — highest cost/uncertainty; gate on profiler data.
   The GIL means threads won't parallelize the pure-Python rasteriser. Options:
   free-threaded CPython 3.14 (band-parallel scanline strips, architecturally aligned but
   maturing) or multiprocessing with `shared_memory` framebuffer strips (pays per-frame
   scene serialization). The QC server + physics MUST stay single-threaded (authoritative,
   deterministic sim); only the renderer is parallelizable. Prototype + measure first.

6. **Menu** — main/pause menu (tkinter UI + game-state machine: start/pause/map-select).
   User-facing polish, lowest technical priority. Sequence last unless the goal is
   shippable feel.



qcc
map tools, bsp, light, vis, quaked
skip tkinter, use ctypes and gdi32
headless mode for testing
headless server mode
bots
review d3d9 on windows

