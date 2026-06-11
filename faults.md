# Faults

All reported faults are fixed (each its own commit + regression test). Root-cause
writeups for the first batch are in `plan.md`.

## Remaining / future work

None tracked. The span/edge (scanline) renderer port — long listed here as the
structural fix for the lift z-fighting — has landed: world and brush geometry now
render through `quake/r_edge.py` (a faithful port of WinQuake's
`r_edge.c`/`d_scan.c` edge-list + surface-stack + span pipeline), and the
coplanar lift/wall shimmer is resolved deterministically by the surface-stack
1/z tie-break (id's `NEARZI_FUDGE`) instead of the old depth-bias stopgap. See
`docs/superpowers/specs/2026-06-11-span-edge-renderer-design.md`.
