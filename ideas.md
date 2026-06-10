# Texture-view bugs — RESOLVED (commit ed93126; test_texture_view_bugs.py covers both)

- **Buttons swap to their lit `+a..+j` alternate textures when pressed.**
  `brush_models()` now reports each entity's `frame`; `_classify_textures` builds
  the base→alternate chains; the textured brush-entity emit substitutes the
  alternate texture when `frame!=0` (Quake's `R_TextureAnimation`).
- **Sky no longer double-layers.** Sky miptexes are split into their two 128x128
  layers (`_split_sky` / `R_InitSky`) and composited each frame into one scrolled
  128-wide tile (`_make_sky` / `R_MakeSky`) — no more side-by-side halves.
- **Palettized rendering / surface cache / `D_CacheSurface` — reviewed, faithful.**
  8-bit palette-indexed framebuffer lit via `colormap.lmp` rows with
  `bytes.translate` (commit 400e2bd); `_surface_cache` is a faithful
  `D_CacheSurface` port, invalidated on lightmap recombine or texture-identity
  swap. No changes were needed; the cache already supports the swaps above.

# Ideas / backlog

Ordered by recommended sequence. Rationale: measure before optimizing; make tuning
interactive; then chase the wins; gate the expensive, uncertain work behind data.

1. **Profiler** — DONE. `quake/perf.py` PROFILER: always-on per-frame section timer
   (server / render / raster / present), EMA-smoothed, P-key HUD bar chart. Not
   `cProfile` (its per-call overhead distorts the per-pixel loops). Everything below
   measures against it.

2. **Console** — DONE, both frontends. Quake-style console + command/cvar/alias table
   (`quake/console.py`). gdi32 (F1) and tkinter (F1 or backtick, commit 714e80a).
   Toggles the render modes, live `zbuf_scale` cvar, noclip, map changes, surfaces
   the profiler HUD; god/give cheats; stdout capture.

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

6. **Menu** — overlay infra DONE, both frontends. The pure menu state-machine
   (`quake/menu.py`) is wired to Esc in gdi32 and tkinter (commit 714e80a): a
   video-options + quit overlay. Remaining is a real *main/start* menu (new-game /
   skill / map-select, pause) on top of that state-machine. User-facing polish,
   lowest technical priority — sequence last unless the goal is shippable feel.



qcc
map tools, bsp, light, vis, quaked
skip tkinter, use ctypes and gdi32
headless mode for testing
headless server mode
bots
review d3d9 on windows

