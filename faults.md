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

5. **Fix the audio teardown errors on close on mac.** FIXED.
   (Correction: `AudioQueueStop(q, 1)` was already the *synchronous* stop —
   inImmediate=true returns once the callback thread has stopped — so the
   "async stop" reading was wrong and the flag was left at 1.) The real gaps:
   the queue was never `AudioQueueDispose`d, shutdown wasn't idempotent, the
   callback had no guard, and teardown relied solely on atexit. Fix: shutdown()
   now stops (1) then disposes (1) once, sets a `_closed` flag the callback
   checks before touching the queue, and the host calls Client.shutdown() on
   quit (main.py / win_gdi.py) with atexit as backstop. Verified on real
   CoreAudio: explicit and atexit-only teardown both exit cleanly across
   repeated runs. Pinned by test_mac_audio_teardown.py.

6. **Particles aren't rendered to the texture buffer.**
   `render_zbuffer()` (render.py:1666) is never given the particle list; only
   flat/wire composite them as frontend overlays (client.py:1029). Fix: port
   `D_DrawParticle` (d_part.c:55-96) — project, distance-scale 1-4 px, z-test
   into the buffer, hooked after sprites (~render.py:2267). HIGH confidence.

7. **Death cam doesn't match Quake.** RESOLVED — no behavioural change needed.
   Re-checked against view.c + runtime: the death cam already matches. The 80°
   roll (V_CalcViewRoll view.c:824), eye drop to view_ofs −8 (PlayerDie
   player.qc:616), hidden weapon and stair-smoothing-off are all correct, and
   adding punchangle on top is *also* correct (view.c:958 adds it regardless of
   death — the earlier "punchangle bug" reading was wrong). Verified at runtime:
   dead view_angles = (pitch, yaw, 80) + punch. The only real view.c divergence
   is head-bob on the dead eye (view.c:893), deliberately omitted because
   self.vel isn't refreshed while dead (it would bob on a stale velocity);
   left as-is. Pinned by test_view_feel.test_dead_view_rolls_to_80.

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

9. **Weird z-fighting in the rendering of the nailgun.** FIXED.
   Ported WinQuake's view-model depth hack (`R_AliasDrawModel` scales `ziscale`
   by 3 for `cl.viewent`, r_alias.c). The zbuf rasterisers now take a `zscale`
   biasing only the z-buffer depth (1/z, and u/z, v/z together so the texel
   recovery is unchanged), leaving the screen projection on the true 1/z; the
   view-model draw passes VIEWMODEL_ZSCALE = 3.0 (render.py). The weapon's
   coaxial barrel triangles separate 3x in depth and stop shimmering, and it
   wins the z-test against world geometry it pokes into. Verified: the bias
   changes the nailgun render but leaves a no-view-model frame byte-identical;
   pinned by test_viewmodel_zbias.py. (World goldens regenerated; unchanged.)
