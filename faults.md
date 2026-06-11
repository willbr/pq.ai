# Faults

All reported faults are fixed (each its own commit + regression test). Root-cause
writeups for the first batch are in `plan.md`. This file now tracks only
remaining/future work.

## Fixed

- **Lift z-fighting (552, 2032, -168)** — bmodel depth bias (BMODEL_ZSCALE, id's
  r_edge.c:493 fudge): coplanar lift faces win the tie deterministically.
- **Blocky lightmaps** — bilinear lightmap interpolation in the lit-surface cache.
- **Particle speeds** — explosion particles seed at ±256 and accelerate outward
  (r_part.c pt_explode/pt_explode2).
- **Stuck on a lift / enemy, wasd dead** — per-mover box-clip expansion
  (SV_HullForEntity), so big monsters don't burrow into the player, plus a
  world-only escape so a trapped player can always walk free.
- **Roof doesn't squash** — SV_PushMove fires .blocked when an entity is pinned
  against the pusher (crush damage).
- **Death cam noclips through the floor** — the corpse sweeps its box and rests
  its box bottom on the floor, keeping the eye above it.

## Remaining / future work

**Span/edge renderer port (r_edge.c + d_scan.c).** The lift z-fighting is patched
with a depth bias, but the structural fix — and a performance win (zero overdraw,
no per-pixel depth compare for world geometry, the per-pixel fill being the
bottleneck) — is to port WinQuake's span/edge renderer. A larger,
renderer-architecture project; see `plan.md`.
