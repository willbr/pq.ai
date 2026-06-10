# Ideas / backlog

Ordered by recommended sequence: chase the wins (profiler's already in place to
measure against), then gate the expensive, uncertain work behind data.

1. **Dynamic resolution, target 60fps** — textured z-buffer mode only (it already renders
   at `1/ZBUF_SCALE`). Closed loop: measure frametime → nudge scale toward a ~16.7ms
   budget with hysteresis + clamped range. Pre-allocate one framebuffer/PhotoImage set per
   scale level (don't reallocate in the hot path). Does NOT help wireframe (Tk-bound);
   its lever is segment/detail culling.

2. **Minimize garbage collection** — continuous companion to 1. Kill per-frame
   allocations in hot loops (temp tuples/lists/arrays), reuse preallocated
   `array`/`bytearray` buffers, consider `gc.disable()` + manual `gc.collect()` at level
   load or raised thresholds. Concrete target: `Mixer.mix()` allocates `out = [0]*N` and a
   fresh `array('h')` every callback. Builds on the existing Canvas line/poly pools.

3. **Multi-threading or processes** — highest cost/uncertainty; gate on profiler data.
   The GIL means threads won't parallelize the pure-Python rasteriser. Options:
   free-threaded CPython 3.14 (band-parallel scanline strips, architecturally aligned but
   maturing) or multiprocessing with `shared_memory` framebuffer strips (pays per-frame
   scene serialization). The QC server + physics MUST stay single-threaded (authoritative,
   deterministic sim); only the renderer is parallelizable. Prototype + measure first.

4. **Main/start menu** — the overlay menu state-machine (`quake/menu.py`) is already
   wired to Esc in both frontends; this is a real main/start menu on top of it (new-game /
   skill / map-select, pause). User-facing polish, lowest technical priority — sequence
   last unless the goal is shippable feel.



qcc
map tools, bsp, light, vis, quaked
skip tkinter, use ctypes and gdi32
headless mode for testing
headless server mode
bots
review d3d9 on windows
