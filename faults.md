# Faults

Triaged 2026-06-11. Faults 1–7 and 9 are done (each its own commit + test);
removed from this list. The full root-cause writeup is in `plan.md`. One fault
remains.

## Z-fighting in the rendering of the lift at 552, 2032, -168

Coplanar moving-bmodel and world faces share the float z-buffer at quarter res.
WinQuake structurally can't fight here: world/bmodels go through the span/edge
renderer, where coplanar ties resolve by sort key and the bmodel wins by rule
(r_edge.c:355-362); only alias models/particles z-buffer. The port's zbuf path
mirrors GLQuake, which has this same bug.

**DECIDED: port the WinQuake span/edge renderer (r_edge.c + d_scan.c)** — fixes
this structurally and removes overdraw + per-pixel depth compares for world
geometry (the per-pixel fill is the bottleneck). A larger, renderer-architecture
project; see `plan.md`. Interim stopgap if needed: ~1.001 bias on bmodel 1/z
(id's own r_edge.c:493 fudge).
