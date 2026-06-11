# Faults

All reported faults are fixed (each its own commit + regression test). Root-cause
writeups for the first batch are in `plan.md`. This file now tracks only
remaining/future work.

## Remaining / future work

**Span/edge renderer port (r_edge.c + d_scan.c).** The lift z-fighting is patched
with a depth bias, but the structural fix — and a performance win (zero overdraw,
no per-pixel depth compare for world geometry, the per-pixel fill being the
bottleneck) — is to port WinQuake's span/edge renderer. A larger,
renderer-architecture project; see `plan.md`.
