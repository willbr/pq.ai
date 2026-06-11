# Gameplay parity plan: Python port vs. original C

Findings from a full review of the Python engine against the GPL reference
(`quake-source/WinQuake` + `qw-qc`), focused on gameplay. Ordered by impact.
Suggested attack order: 1.1 + 1.2 (campaign), then 1.3 (combat feel), then
2.1 (feedback), then 1.4, then 2.2 + 2.3.

## Tier 1 — game rules that are broken or absent

### 1.1 Inventory carry-over between levels — DONE
- `_pf_setspawnparms` is a no-op (`sv.py:1414`); `parm1–parm16` globals are
  never written or read, so QC's `SetChangeParms`/`DecodeLevelParms`
  (client.qc:62-123, host_cmd.c) never persist anything.
- Every `changelevel` resets the player to shotgun + 25 shells. The biggest
  gameplay gap in the port.
- Done: `Server.save_spawn_parms()` ports `SV_SaveSpawnparms` (runs QC
  `SetChangeParms`, returns parm1..16); `spawn_player(..., parms=)` writes
  them back and runs QC `SetNewParms`/`DecodeLevelParms` instead of the
  hard-coded loadout; `setspawnparms` builtin implemented; the host carries
  parms across the Server rebuild in `_change_level` (death restart keeps
  the entry loadout, `map` command resets, matching Host_Restart_f /
  Host_Map_f). Tested in `test_spawn_parms.py`.

### 1.2 serverflags / episode sigils — DONE (with 1.1)
- `serverflags` now persists across changelevel: `save_spawn_parms` latches
  the global, the host hands it to the next `Server(serverflags=...)`, and
  `load_level` seeds the global before spawn functions run (SV_SpawnServer).
  QC sets the rune bits itself; `DecodeLevelParms`' start.bsp episode reset
  works because the world edict's model is the map path.
- Related: `SPAWNFLAG_NOT_DEATHMATCH` (2048) is defined but never checked in
  `_inhibited()` (`sv.py:359`) — only matters if DM is ever a goal.

### 1.3 MOVETYPE_STEP physics (monster gravity/knockback) — DONE
- Constant (4) not even defined in `sv.py:44-50`; no handler in `run_frame`.
- Monsters navigate fine (walkmove/movetogoal → the faithful `SV_movestep`
  port at `sv.py:1899`, and thinks run regardless of movetype), but
  `SV_Physics_Step` (sv_phys.c:1468-1497) is missing:
  - explosion/shotgun knockback on monsters does nothing (QC sets
    `velocity`; nothing integrates it),
  - monsters knocked off a ledge or killed mid-air never fall,
  - no `SV_CheckWaterTransition` splash.
- Fix: port the simple WinQuake `SV_Physics_Step`: if not `FL_ONGROUND`,
  apply gravity + `fly_move`, re-derive onground; then run think.

### 1.4 pointcontents builtin stubbed — DONE
- Always returns `CONTENTS_EMPTY` (`sv.py:1808`). Player waterlevel works
  only because the host calls `update_player_water()` directly.
- Breaks QC's own uses: lightning-gun discharge underwater
  (`W_FireLightning`), fish/scrag water checks.
- Fix: route to `phys` hull-0 point contents (the machinery already exists —
  physics uses it for the player).

### 1.5 Save/load games — DONE
- Nothing corresponding to `Host_Savegame_f`/`Host_Loadgame_f`
  (host_cmd.c:465-684, `ED_Write`/`ED_ParseGlobals`/`ED_ParseEdict`).
- Combined with 1.1, every session is one map run.
- Fix: serialize globals + all edict fields by name (the .sav text format is
  simple and the field tables in `progs.py` make it mechanical), plus
  mapname/time/skill/lightstyles; on load, skip spawn functions.

### 1.6 aim() autoaim stub — DONE
- Returns `v_forward` (`sv.py:1424`); original `PF_aim` (pr_cmds.c) does
  vertical aim assist toward the best target. Matters for shotgun/nailgun
  shots at monsters above/below; less critical with mouselook.

### 1.7 StartFrame never called — DONE
- id's QC `StartFrame` re-reads `teamplay`/`skill` cvars each frame and bumps
  `framecount`; without it, changing skill mid-game never propagates.
- Fix: one `vm.execute()` at the top of `run_frame()` (`sv.py:407`).

## Tier 2 — gameplay feedback that's missing (the game plays blind)

### 2.1 Screen flashes / palette tints — DONE
- view.c:316-473 + `V_UpdatePalette` (view.c:527-672): red damage flash,
  gold bonus/pickup flash, quad/pent/ring/suit powerup tints,
  water/slime/lava underwater tint.
- Damage and powerups are currently invisible except as HUD numbers.
  Highest bang-for-buck client work: a full-screen blend in the rasteriser
  (or a tint pass on the framebuffer) driven from player fields each frame.

### 2.2 Dynamic lights — DONE
- cl_main.c:317-365 (`CL_AllocDlight`): rocket glow, explosion flash
  (radius 350, 0.5s), `EF_MUZZLEFLASH`, `EF_BRIGHTLIGHT`/`EF_DIMLIGHT`
  powerup glow. Lighting is static lightmaps only.
- (Lightstyle animation *is* implemented — `render.py:1232`
  `_animate_lightmaps`.)

### 2.3 Lightning beams not rendered — DONE
- cl_tent.c:216-231 (`CL_ParseBeam`, `progs/bolt.mdl`): the lightning gun and
  Shambler attack are completely invisible. TE messages beyond particle
  bursts are dropped in `sv.py`'s Write* decoding.

### 2.4 No .spr sprite support
- `s_explod.spr` explosion sprite etc. (r_sprite.c). Particle bursts and
  rocket/grenade/gib trails ARE implemented (`sv.py:1270-1396`); impact
  sprites and explosion billboards are not.

### 2.5 View feel — DONE (idle intermission sway intentionally skipped)
- Missing: strafe roll (`V_CalcRoll`, view.c:81-103), damage kick
  (`V_ParseDamage` pitch/roll punch, view.c:316-380), `.punchangle` weapon
  recoil, landing dip. Done: view bob (`client.py:814`), death camera and
  intermission camera (`client.py:608-626`).

### 2.6 HUD information gaps — DONE
- Text health/armor/ammo works (`client.py:696`), but there is no
  keys/sigils/items readout — you cannot see whether you hold the silver/gold
  key. That's gameplay information, not just sbar.c cosmetics.

## Tier 3 — smaller fidelity gaps

### 3.1 Audio — DONE (CD music still absent; channel-0 rule was already correct)
- Done: spatialization, attenuation, per-entity channel replace
  (`snd.py:155-205`).
- Missing: dedicated ambient channels gated by leaf contents (water/sky
  ambience plays everywhere — snd_dma.c ambient slots 0-3), the
  channel-0-never-overrides rule, CD music.

### 3.2 Cheats / console commands — DONE
- Have: `god`, `give`, `noclip`, `map`. Missing: `notarget`, `fly`, `kill`,
  impulse 9/255 (client only sends impulses 1-8). `noclip` doesn't set the
  edict's `movetype`, so QC is unaware of it.

### 3.3 VM / edict hygiene — DONE (div-by-zero divergence kept, documented)
- `alloc_edict` reuses freed slots immediately — no 0.5s `freetime` guard, no
  reserved client slots (pr_edict.c:87-112). Latent bug under fast
  projectile churn.
- `free_edict` zeroes every field rather than C's selective `ED_Free` clear.
- No `SV_CheckVelocity` (NaN check + ±2000 clamp).
- Division by zero returns 0.0 instead of C's IEEE NaN/Inf
  (`pr_exec.py:245`) — arguably safer; documented divergence.

## Verified faithful (no action needed)

Player movement chain (friction / accelerate / air-accelerate / water /
stairs / unstick), `SV_FlyMove` plane clipping, pushers with riders and
crush, toss/bounce projectiles, monster navigation (`SV_CheckBottom` /
`SV_NewChaseDir` / `SV_StepDirection`), trigger touching with correct
self/other ordering, VM opcode set + call/locals stack, entity spawning with
skill inhibit, intermission flow and stats, kill/secret counters (QC
increments those globals itself), `centerprint`, lightstyle animation.
