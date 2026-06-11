# faults.md triage: root causes and fixes

Findings from reviewing each fault in `faults.md` against the code and the GPL
reference (`quake-source/WinQuake` + `qw-qc`). Ordered as in faults.md.
Each was investigated independently; file:line citations verified.

## 1. View model mid-screen on steps / lifts — HIGH confidence

The stair-smoothing offset is applied to the eye only, after the gun origin
is already baked, so the camera lags downward while the weapon stays at the
unsmoothed height — it drifts up the screen on every step-up / lift ride.

- `client.py:974` — `eye, gun_org = view_origins(...)` computed unsmoothed
- `client.py:982` — `eye_z_offset` added to the eye **only**, after gun_org is frozen
- Reference: `view.c` V_CalcRefdef applies the `oldz` smoothing to **both**
  `r_refdef.vieworg[2]` and `view->origin[2]` symmetrically (view.c:975-976)

**Fix:** apply `self.eye_z_offset` to `gun_org[2]` as well, right where the eye
gets it (client.py:982).

## 2. Demons jump but can't land — HIGH confidence (verified in source)

`_physics_step` (`quake/sv.py:632-676`) integrates the falling monster and even
sets FL_ONGROUND, but never fires the touch function on impact. The demon's
leap relies on `Demon_JumpTouch` (set as `.touch` during the jump) to exit the
jump state; it never runs, so the demon hangs in the jump pose until demon1.qc's
3-second retry re-jumps it. Compare `_physics_toss` (`quake/sv.py:599`), which
correctly calls `self._sv_impact(num, hit)` — and C `SV_FlyMove` (sv_phys.c),
which calls `SV_Impact` on every collision inside the loop.

**Fix:** in the `_physics_step` collision loop, after a `tr.fraction < 1.0` hit,
call `self._sv_impact(num, tr.ent)` (then bail if `vm.free[num]`), mirroring
`_physics_toss`.

## 3. Player + monster walk into each other and both get stuck — HIGH confidence

Monster moves don't clip against the player at all. The port conflates two
distinct SV_Move concepts into one `record` flag: `physics.move(record=False)`
(used by monster movestep via `quake/sv.py:2338`) skips box-entity clipping
entirely (`quake/physics.py:305`), whereas in the C source `SV_movestep`
(sv_move.c:138,163) calls `SV_Move(..., MOVE_NORMAL, ent)` — it *does* clip
against the player's box, using `passedict` only to skip self-collision
(world.c:827). So a monster steps into the player's box, the entities overlap,
and from then on the player's own traces start solid (`physics.py:310-311`)
and every move fails — both stuck.

**Fix:** add a `passedict` parameter to `physics.move()` / `_trace_box`; keep
box clipping on for monster moves, skipping only the moving entity itself; make
`record` control touch recording only. Monster movestep passes its own edict.

## 4. Can't move (WASD) while shooting the nailgun — HIGH confidence

The player clips against his own nails. Mouselook unaffected (angles don't
collide) and other guns fine (shotgun is hitscan; rockets/grenades fire ~1/s
and clear the player hull within a frame) — only the nailgun puts a fresh
projectile at the muzzle every 0.1s.

- `quake-source/qw-qc/weapons.qc:724-730` — `launch_spike` spawns each nail
  SOLID_BBOX with `owner = self`, origin at the muzzle (~8 units ahead)
- `quake/sv.py:919-938` — `solid_box_entities(ignore=player)` skips only the
  player edict itself; the player's own missiles are included
- the box trace expands the nail's zero-size bbox by the player hull, so each
  fresh nail is a player-sized obstacle directly ahead — walking forward while
  firing is blocked nearly every frame
- Reference: `world.c:849-855` SV_ClipToLinks — "don't clip against own
  missiles" (`touch->v.owner == passedict`) and "don't clip against owner"
  (`passedict->v.owner == touch`)

**Fix:** in `solid_box_entities` (or the box-clip path once fault 3's
`passedict` lands), skip entities whose `.owner` is the ignored edict, plus the
reverse check, exactly as world.c does. Note faults 3 and 4 land in the same
code: both are missing pieces of SV_ClipToLinks' skip logic.

## 5. Audio teardown errors on close (mac) — HIGH confidence

`CoreAudioBackend.shutdown` (`mac.py:136-140`) calls
`AudioQueueStop(self._queue, 1)` — the `1` makes the stop **asynchronous**, so
the callback thread can fire again after shutdown returns, into a
half-torn-down interpreter (the documented nondeterministic segfault), and the
queue is never `AudioQueueDispose`d. The callback (`mac.py:127-134`) calls
`self.mixer.mix(...)`, which crashes/raises if it runs after GC. Teardown is
triggered only by the atexit hook; `main.py` does no explicit audio shutdown
before `root.destroy()`.

**Fix:** stop synchronously (`AudioQueueStop(q, 0)` — wait flag per Apple docs:
0 = stop immediately/synchronously) then `AudioQueueDispose(q, 1)`; declare
argtypes/restype for Dispose in `_open_stream`; call `shutdown()` explicitly
from the frontend quit path before `root.destroy()`, keep atexit as backstop,
and make shutdown idempotent.

## 6. Particles not rendered in the texture buffer — HIGH confidence

The zbuf path simply never receives particles. Flat/wire modes get them as
screen-space overlay sprites (`client.py:1029` → RenderFrame, frontend
composites them), but `render_zbuffer()` (`quake/render.py:1666`) takes only
`sprites=` (.spr billboards, render.py:2242-2266) and has no particle loop, so
the framebuffer is finished without them.

**Fix:** port `D_DrawParticle` (d_part.c:55-96): pass the live particle list
into `render_zbuffer`, and per particle do near-clip, perspective project,
distance-scaled pixel size (1-4 px at full res, scaled by ZBUF_SCALE), and a
per-pixel z-test against the existing z-buffer. Hook in after sprites, before
the view model (~render.py:2267).

## 7. Death cam doesn't match Quake — RESOLVED, no change needed

On closer reading the original finding was wrong. `V_CalcViewRoll` (view.c:824)
only sets ROLL = 80 and returns; punchangle is added *separately and
unconditionally* back in V_CalcRefdef (view.c:958), so applying punchangle on
top of the death roll is correct, not a bug. The port already does exactly
this, and runtime check confirms a dead player's view_angles = (pitch, yaw, 80)
+ punch, eye dropped to view_ofs z = −8 (PlayerDie player.qc:616), weapon
hidden, stair smoothing off, mouselook live. All correct.

The single genuine divergence from view.c is the head-bob the live path adds to
the eye (view.c:893, `vieworg[2] += cl.viewheight + bob`): the port omits it
when dead. Left omitted on purpose — `self.vel` is not refreshed while dead
(the move is skipped), so feeding it to V_CalcBob would sway the corpse view on
a stale velocity, which is worse than no bob. Faithfully matching it would mean
tracking the corpse's velocity into self.vel; deferred as not worth it.

Pinned by `test_view_feel.test_dead_view_rolls_to_80` so it can't regress.

## 8+9. Z-fighting: lift bmodel and nailgun view model — root causes differ

**Nailgun view model (HIGH confidence):** WinQuake draws the view model with a
depth hack — `R_AliasDrawModel` (r_alias.c) multiplies `ziscale` by 3 for
`cl.viewent`, compressing the weapon's depth so it always wins against nearby
world geometry and its own coaxial barrel triangles stop shimmering. The port
draws the view model through the same raster path as any entity with no bias
(`quake/render.py:2270-2275`). **Fix:** pass `is_viewmodel=True` down to the
alias rasteriser and scale the interpolated `iz` by 3.0 before the depth test.

**Lift at (552, 2032, -168) (MEDIUM confidence on mechanism):** the moving
bmodel's faces are coplanar with adjacent world faces and both go through the
same float z-buffer at quarter resolution, so per-pixel 1/z gradients from the
two faces interleave — classic coplanar z-fighting. WinQuake *structurally*
cannot z-fight here: world + bmodel surfaces go through the span/edge renderer
— surfaces get sort keys in BSP front-to-back order (r_bsp.c:633; bmodels
share their leaf's key, r_bsp.c:524-525), and coplanar ties are resolved
categorically in the span stack: the bmodel wins (r_edge.c:355-362). Only
alias models/sprites/particles use the z-buffer (written via D_DrawZSpans).
The port's zbuf path is instead a GLQuake port (render.py:2099-2104 cites
gl_rsurf.c) — and original GLQuake has this same lift/door z-fighting.

**DECIDED: port the span/edge renderer (r_edge.c + d_scan.c).** Make the
textured mode mechanism-faithful to WinQuake: emit clipped edges during the
BSP walk, active-edge-list scan per scanline with the keyed surface stack,
one surface per span, draw spans, then write z-spans (D_DrawZSpans) so alias
models/sprites/particles still z-test as today. Besides eliminating the
z-fighting structurally, this gives zero overdraw and removes the per-pixel
depth compare for world geometry — a direct attack on the port's stated
bottleneck (the Python per-pixel fill). This is a renderer-architecture
project, not a spot fix; track it separately from the small fixes here.
(Interim option if a stopgap is wanted: bias bmodel 1/z by ~1.001 at the
depth test — the same fudge id used for bmodel-vs-bmodel ties, r_edge.c:493.)

## Suggested order

2 (demon land), 3 (mutual stuck) and 4 (own-missile clipping) are
gameplay-breaking and well-understood — do first; 3 and 4 share the
SV_ClipToLinks skip logic, so do them together. Then 1 and 7 (small client view
fixes), 5 (mac teardown), 9 (viewmodel ziscale), 6 (particles). 8 is its own
project (span/edge renderer port) — schedule separately.
