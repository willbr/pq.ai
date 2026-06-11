# Faults

All reported faults are fixed (each its own commit + regression test). Root-cause
writeups for the first batch are in `plan.md`.

## Remaining / future work

None tracked. The span/edge (scanline) renderer port — long listed here as the
structural fix for the lift z-fighting — has landed: world and brush geometry now
render through `quake/r_edge.py` (a faithful port of WinQuake's
`r_edge.c`/`d_scan.c` edge-list + surface-stack + span pipeline).

Occlusion is resolved by id's BSP-key machinery, faithfully ported: the world
walk assigns the monotonic front-to-back `r_currentkey` (`R_RecursiveWorldNode`),
and brush models are keyed into that order by the world leaf they occupy —
single-leaf models inherit the leaf key (`R_SplitEntityOnNode2` /
`R_DrawSubmodelPolygons`), straddling models are BSP-clipped per-fragment
(`R_RecursiveClipBPoly`) so each fragment sorts correctly in each leaf it
crosses. The surface-stack 1/z tie-break (`NEARZI_EPS`) only breaks same-key
(coplanar) ties, which is what kills the lift/wall shimmer — no depth bias. An
earlier uniform-key simplification (one key for all surfaces, 1/z-only ordering)
was abandoned because it produced an incorrect z-buffer that let func walls /
lifts / stairs and the items behind them render see-through. See
`docs/superpowers/specs/2026-06-11-span-edge-renderer-design.md`.
