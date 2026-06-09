# Per-frame section profiler

## Goal

Add lightweight, always-on per-frame section timing so we can see where each
frame's time goes (sim vs render vs blit) and chase the real bottleneck before
attempting any optimisation. This is `ideas.md` item #1 — the measurement
substrate the later perf work (dynamic resolution, GC tuning, threading)
depends on.

Explicitly **not** `cProfile`: its per-call overhead distorts the per-pixel
loops. We measure a handful of coarse sections with `time.perf_counter()`,
which costs nanoseconds per frame.

## Module: `quake/perf.py`

Pure stdlib, no OS/UI imports, so it loads cleanly from inside the `quake`
package (relative import in `render.py`) and from the root frontends (absolute
import). Single-thread by design — only the frame thread touches it; the audio
thread never does.

`Profiler` takes an injectable clock — `Profiler(clock=time.perf_counter)` —
so tests can drive it with a deterministic counter instead of real time. The
module-level singleton `PROFILER` uses the default `perf_counter`.

The class:

- `section(name)` — context manager (`with PROFILER.section("render"):`) timing
  the block with `perf_counter()` and adding the elapsed seconds into a named
  accumulator for the current frame. **Nesting is allowed** so `"raster"` can
  sit inside `"render"`; each name keeps its own bucket independent of nesting.
- `frame_end()` — called once per frame. Rolls each accumulator into an
  **EMA-smoothed** millisecond value using the same 0.9/0.1 weighting as the
  existing `fps`, then clears the accumulators for the next frame. Sections not
  hit this frame decay toward 0.
- `report()` — returns the smoothed per-section ms plus a total, formatted as a
  one-line HUD string (e.g. `srv 2.1  rnd 9.8 (ras 7.4)  pre 3.2  tot 15.1`).

Timing is always on; only the *display* is toggled (see below). A few
`perf_counter` calls per frame is negligible, and always-on data avoids
warm-up artefacts when the HUD is first shown.

## Sections

| Section   | Where wrapped                                           |
|-----------|---------------------------------------------------------|
| `server`  | `client.frame()` around the QC tick + physics           |
| `render`  | `client.frame()` around the `rend.render_*` call        |
| `raster`  | nested in `render_zbuffer` **and** `render_shaded`, around the face-emission/fill region (after PVS/visible-leaf setup) |
| `present` | each frontend, around the blit                          |

`render_zbuffer` transforms vertices lazily *inside* the draw loops, so
cull+transform is not a separable phase; the `raster` timer wraps the
emission/fill region, and `render - raster` then reads as setup/cull. Wire mode
is present-bound (Tk/GDI line drawing), so it gets no nested timer.

The HUD string is built mid-frame, before present, so the breakdown shown is
the **previous completed frame's** smoothed values — a uniform one-frame lag
across all sections that the EMA makes invisible.

## Toggle and display

- A new one-shot command `"prof"` joins the existing
  `noclip/flat/zbuf/texture` set in `InputState.commands`.
- `client.frame()` handles it by flipping `self.show_prof` (default `False`).
  When on, it appends `PROFILER.report()` to the existing HUD overlay — so
  **both frontends display it for free** through the existing overlay path; no
  per-frontend drawing code.
- Key binding per frontend: **P** in `main.py` (`_keydown` → queue `"prof"`),
  `VK_P` added to `win_gdi.py`'s `COMMAND_KEYS`.

## Frame wiring

Per frame the order is: frontend computes `dt` → `client.frame()` (times
`server`, `render`/`raster`; builds HUD from the *previous* frame's smoothed
report) → frontend times `present` around the blit → frontend calls
`PROFILER.frame_end()` to roll all buckets. One `frame_end()` call site per
frontend.

## Testing

`test_perf.py` — standalone script following the repo convention (functions
named `test_*`, `__main__` runs them and prints `OK`). Pure logic, no stack
boot, fast:

- a single section accumulates the time spent inside it
- nested sections each record independently (inner time also counted in inner
  bucket, not subtracted from outer)
- multiple `section()` entries with the same name within a frame sum
- `frame_end()` EMA-smooths toward the latest frame and resets accumulators
- a section absent for a frame decays toward 0
- `report()` contains every active section name and a total

Timing assertions use a fake/injected clock (a counter the test advances)
rather than real sleeps, so the test is deterministic and instant.

## Out of scope (YAGNI)

cProfile/py-spy integration, the dynamic-resolution loop (idea #3), GC tuning
(#4), and threading (#5). This change is only the measurement substrate.
