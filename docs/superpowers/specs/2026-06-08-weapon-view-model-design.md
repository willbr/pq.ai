# First-person weapon view model

## Goal

Draw the player's first-person weapon (Quake's `R_DrawViewModel`: `progs/v_shot.mdl`
etc.) fixed to the camera, bobbing with movement and animating when fired. Present in
both the wireframe and flat-shaded renderers.

## Source of truth

The running QuakeC already maintains, on the player edict:

- `weaponmodel` (string field, offset 51) — the `progs/v_*.mdl` path for the current weapon
- `weaponframe` (float field, offset 52) — the view-model animation frame

So we read what the real game code computes rather than mapping the `weapon` flag
ourselves. All eight `v_*.mdl` files are present in the shareware pak.

## Components

### `sv.py` — expose weapon state
- Add `weaponmodel`, `weaponframe` to `_FIELDS`.
- New `Server.view_weapon() -> (path:str, frame:int) | None`: reads the `weaponmodel`
  string (`fget_i` → `pr.string`) and `weaponframe` (`fget_f`) off the player edict.
  Returns `None` when there is no player or the path is empty.

### `main.py` — build the viewmodel each tick
- Lazy model cache `self._vmodels[path] = Mdl(pak.read(path), palette)` (the `v_` models
  are not all in `model_precache`, so load on demand and keep).
- After the QC frame, before rendering:
  - `path, frame = sv.view_weapon()`; look up / load the `Mdl` (skip if missing).
  - `origin = eye + bob`, `angles = (-self.pitch, self.yaw, 0.0)` — negating pitch makes
    `model_axes()` align exactly with the camera basis, so the gun rides the view.
  - `verts = mdl.frame_verts(frame, sv.time)`.
  - Pass `view_model=(mdl, verts, origin, angles)` to the renderer.

### Weapon bob — port `V_CalcBob`
Quake constants `cl_bob=0.02`, `cl_bobcycle=0.6`, `cl_bobup=0.5`, clamp `[-7, 4]`.
Phase from a wall-clock bob timer; amplitude from horizontal speed
`hypot(vel[0], vel[1])`. Apply along the view forward (`*0.4`) and to `z` (`+bob`),
as in `V_CalcRefdef`.

### `render.py` — final viewmodel pass (both renderers)
Add an optional `view_model=None` arg to `render()` and `render_shaded()`. Drawn as a
final pass that **skips PVS culling** (always visible) and, in shaded mode, is emitted
**after** the painter walk so it overdraws the world (no z-buffer → draw order is
occlusion). Reuses the existing alias transform + `skin_color`/directional-light
shading. Wireframe appends the gun's triangle edges (draw order irrelevant).

## Testing

No automated suite exists; the feature is visual.
- Smoke check: `sv.view_weapon()` returns `progs/v_shot.mdl` and a frame on a spawned
  player; both renderers accept `view_model=` without error.
- Manual: `python3 main.py e1m1` — gun visible bottom-centre, bobs while walking,
  frame animates when firing, present in both `[F]` modes.

## Note

The `(-pitch, yaw, 0)` sign gives a rigidly view-attached gun (cleaner than Quake's
subtle pitch quirk). Confirm visually on first run; flip the sign if it reads wrong.
