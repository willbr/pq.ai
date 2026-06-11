# Faults

Triaged 2026-06-11 — full root-cause writeup with file:line evidence in
`plan.md`. Suggested fix order: 2, 3, 4 (gameplay-breaking; 3 and 4 share the
SV_ClipToLinks skip logic), then 1, 7, 5, 9, 6. 8 is decided as a span/edge
renderer port — its own project.

1. **Walking up steps, or riding lifts up, the view model is in the middle of
   the screen.**
   Stair-smoothing `eye_z_offset` is applied to the eye only (client.py:982)
   after `gun_org` is already computed (client.py:974); view.c:975-976 offsets
   both. Fix: add the offset to `gun_org[2]` too. HIGH confidence.

2. **Demons jump but can't land.**
   `_physics_step` (quake/sv.py:632-676) never fires touch on impact, so
   `Demon_JumpTouch` never runs and the demon hangs until QC's 3s re-jump.
   Fix: call `self._sv_impact(num, tr.ent)` in the collision loop (and bail if
   freed), as `_physics_toss` already does at sv.py:599. HIGH confidence.

3. **Walking forward into a monster can cause us both to get stuck.**
   Monster moves skip box-entity clipping entirely (`move(record=False)`,
   physics.py:305) instead of clipping against the player with a `passedict`
   self-exclusion (world.c:827, sv_move.c:138). Monster steps into the player,
   they overlap, then every trace starts solid (physics.py:310-311). Fix: add
   `passedict` to `physics.move()`; `record` should only control touch
   recording. HIGH confidence.

4. **I can't move (WASD) while shooting the nailgun — mouselook still works,
   other guns fine.**
   The player clips against his own nails. `launch_spike` makes each spike
   SOLID_BBOX with `owner = self` at the muzzle (weapons.qc:724-730);
   `solid_box_entities(ignore=player)` (quake/sv.py:919-938) skips only the
   player, so every fresh nail — expanded to a player-sized box by the trace —
   blocks the move 8 units ahead, respawned every 0.1s. SV_ClipToLinks
   (world.c:849-855) skips entities owned by the mover ("don't clip against
   own missiles") and the mover's owner. Other guns: shotgun is hitscan,
   rockets/grenades fire ~1/s and clear the hull in a frame. Fix: in
   `solid_box_entities`, also skip entities whose `.owner == ignore` (and the
   reverse owner check), per world.c. HIGH confidence.

5. **Fix the audio teardown errors on close on mac.**
   `shutdown()` (mac.py:136-140) uses async stop — `AudioQueueStop(q, 1)` — so
   the callback thread can fire into a half-dead interpreter (also the
   documented test segfault), and the queue is never disposed. Fix: synchronous
   stop + `AudioQueueDispose`, called explicitly from the quit path before
   `root.destroy()`, atexit as backstop, idempotent. HIGH confidence.

6. **Particles aren't rendered to the texture buffer.**
   `render_zbuffer()` (render.py:1666) is never given the particle list; only
   flat/wire composite them as frontend overlays (client.py:1029). Fix: port
   `D_DrawParticle` (d_part.c:55-96) — project, distance-scale 1-4 px, z-test
   into the buffer, hooked after sprites (~render.py:2267). HIGH confidence.

7. **Death cam doesn't match Quake.**
   Punchangle is added on top of the 80° death roll (client.py:458-465);
   `V_CalcViewRoll` (view.c:822-826) assigns ROLL=80 and returns. Everything
   else (weapon hidden, eye at view_ofs −8, stair smoothing off) already
   matches. Fix: when dead, set `(pitch, yaw, 80.0)` and return early. HIGH
   confidence.

8. **Weird z-fighting in the rendering of the lift at 552, 2032, -168.**
   Coplanar moving-bmodel and world faces share the float z-buffer at quarter
   res. WinQuake structurally can't fight here: world/bmodels go through the
   span/edge renderer, where coplanar ties resolve by sort key and the bmodel
   wins by rule (r_edge.c:355-362); only alias models/particles z-buffer. The
   port's zbuf path mirrors GLQuake, which has this same bug. **DECIDED: port
   the WinQuake span/edge renderer (r_edge.c + d_scan.c)** — fixes this
   structurally and removes overdraw + per-pixel depth compares for world
   geometry (the per-pixel fill is the bottleneck). Bigger project; see
   plan.md. Interim stopgap if needed: ~1.001 bias on bmodel 1/z (id's own
   r_edge.c:493 fudge).

9. **Weird z-fighting in the rendering of the nailgun.**
   Missing WinQuake view-model depth hack: `R_AliasDrawModel` scales `ziscale`
   by 3 for `cl.viewent` (r_alias.c); the port rasters the weapon like any
   entity (render.py:2270-2275). Fix: `is_viewmodel=True` → multiply `iz` by
   3.0 before the depth test. HIGH confidence.
