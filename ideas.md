tidy tests into a tests folder

# Ideas / backlog

Ordered by recommended sequence: chase the wins (profiler's already in place to
measure against), then gate the expensive, uncertain work behind data.

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
headless mode for testing
headless server mode
bots

review using classes, aren't they bloody slow in pytohn?

headless server with bots

