things to fix in texture view
* button textures dont change when you press them
* sky texture is double layered

review if we're using a palleted rendering like quake1
review what's cached
review D.CacheSurface

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

