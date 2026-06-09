# gdi32 Windows Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken Windows raw-mouselook by giving Windows a pure-gdi32 frontend with its own message loop, sharing all game logic with the tkinter frontend through a new UI-agnostic `Client` core.

**Architecture:** Extract a `Client` core (`client.py`) holding the engine stack and all camera/player/game state, exposing `frame(dt, input) -> RenderFrame`. Two thin frontends — `main.py` (tkinter, all platforms) and `win_gdi.py` (Windows) — fill an `InputState` from native events, run their own native loop, and draw the returned `RenderFrame`. `InputState` and `RenderFrame` are the only shared contracts.

**Tech Stack:** Python 3.13 stdlib only; tkinter (Tk frontend); ctypes → user32/gdi32 (Windows frontend, via existing `win_ui.py`). No build step; standalone `test_*.py` / `smoke_*.py` scripts that print `OK`.

**Spec:** `docs/superpowers/specs/2026-06-09-gdi32-windows-frontend-design.md`

---

## File Structure

- `client.py` (**create**) — `InputState`, `RenderFrame` dataclasses + `Client` core (engine stack + all game state + `frame()`). Imports only `quake.*` and stdlib; no tkinter, no ctypes.
- `main.py` (**modify**) — reduce `App` to a thin tkinter frontend over `Client`: `after()` loop, Tk events → `InputState`, draw `RenderFrame` via Canvas/`PhotoImage`. Remove the raw-mouselook, GDI-present, and `PQ_DIAG` code (moves to / superseded by `win_gdi.py`).
- `win_gdi.py` (**create**) — Windows frontend: owns the `PeekMessage` loop + raw mouselook + cursor grab (from `spike_gdi.py`), drives `Client.frame()`, draws `RenderFrame` via `GdiBlitter`.
- `win_ui.py` (**modify**) — add `GdiBlitter` vector/particle drawing (`Polyline`, `Polygon`, `FillRect`) used by `win_gdi.py`. Keep existing blit/text/raw helpers.
- `test_client.py` (**create**) — headless `Client.frame()` test.
- `smoke_win_gdi.py` (**create**) — live smoke for the Windows frontend (window + a few real frames + shutdown).
- `spike_gdi.py`, `smoke_spike.py` (**delete** in Stage 4) — superseded by `win_gdi.py`.

---

## STAGE 1 — Extract `Client`, refactor `main.py` onto it (zero behavior change)

Goal: all platforms still run tkinter exactly as today, but game logic now lives in `Client`. The broken raw/GDI/diag code is removed from `main.py`; Tk uses the warp mouselook everywhere.

### Task 1.1: Define the `InputState` and `RenderFrame` contracts

**Files:**
- Create: `client.py`
- Test: `test_client.py`

- [ ] **Step 1: Write the failing test for the contracts**

Create `test_client.py`:

```python
"""Headless tests for the UI-agnostic Client core and its two data contracts.
Boots the full stack against the shareware pak (like the other test_*.py), so it
fails without quake-shareware/id1/pak0.pak."""

import client


def test_inputstate_defaults_are_neutral():
    inp = client.InputState()
    assert inp.move_forward == 0.0 and inp.move_strafe == 0.0 and inp.move_up == 0.0
    assert inp.turn == 0.0 and inp.look_dx == 0.0 and inp.look_dy == 0.0
    assert inp.run is False and inp.fire is False and inp.impulse == 0
    assert inp.commands == frozenset()


def test_renderframe_holds_mode_and_overlays():
    rf = client.RenderFrame(mode="wire", segs=[(0, 0, 1, 1)],
                            overlays=[(8, 8, "hi", (0, 255, 0), "nw")],
                            crosshair=(50, 50))
    assert rf.mode == "wire"
    assert rf.segs == [(0, 0, 1, 1)]
    assert rf.overlays[0][2] == "hi"


if __name__ == "__main__":
    test_inputstate_defaults_are_neutral()
    test_renderframe_holds_mode_and_overlays()
    print("OK")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python test_client.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'client'`.

- [ ] **Step 3: Write the contracts**

Create `client.py` with the two dataclasses (full `Client` class added in 1.2):

```python
"""UI-agnostic game client: owns the engine stack (Pak/Bsp/Renderer/Physics/Server)
and all camera/player/game state, and turns one frame of input into a RenderFrame
the frontend draws. Imports only quake.* and stdlib -- no tkinter, no ctypes -- so
both the tkinter frontend (main.py) and the gdi32 frontend (win_gdi.py) share it."""

from dataclasses import dataclass, field


@dataclass
class InputState:
    """One frame of intent, filled by the frontend from native events. Read-only
    to Client. look_dx/dy are mouse counts since the last frame, sent only while
    mouselook is engaged. commands holds one-shot edge-triggered toggles fired this
    frame, a subset of {'noclip','flat','zbuf','texture'}."""
    move_forward: float = 0.0
    move_strafe: float = 0.0
    move_up: float = 0.0
    turn: float = 0.0
    look_dx: float = 0.0
    look_dy: float = 0.0
    run: bool = False
    fire: bool = False
    impulse: int = 0
    commands: frozenset = frozenset()


@dataclass
class RenderFrame:
    """What Client.frame() returns; the frontend draws it. mode is 'wire'|'flat'|
    'zbuf'. Exactly one of segs/polys/framebuffer is set per mode. overlays are
    (x, y, text, (r,g,b), anchor) with anchor in {'nw','center','sw'}."""
    mode: str
    segs: list = None                       # mode 'wire': line segments
    polys: list = None                      # mode 'flat': (points, color)
    framebuffer: tuple = None               # mode 'zbuf': (bytes, w, h)
    particles: list = field(default_factory=list)
    overlays: list = field(default_factory=list)
    crosshair: tuple = (0, 0)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python test_client.py`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add client.py test_client.py
git commit -m "Add Client InputState/RenderFrame contracts"
```

### Task 1.2: `Client` boot + state (move engine setup out of `App`)

**Files:**
- Modify: `client.py`
- Reference (source of moved code): `main.py` `App.__init__` (engine/state parts), `App._load_map` (`main.py:245-328`), `App._change_level`, `App._move`, `App._wishmove`, `App._calc_bob`, `App._alias_ents`, `App._bsp_ents`, `App._sync_from_player`, `App._view_model`, and the helper functions `view_origins`, `spin_yaw` (`main.py:86-105`).
- Test: `test_client.py`

- [ ] **Step 1: Write the failing test for boot + state**

Add to `test_client.py` (and add the calls to `__main__`):

```python
def test_client_boots_e1m1_with_spawn_and_viewport():
    c = client.Client("e1m1")
    c.resize(800, 600)
    assert len(c.pos) == 3                 # player origin from the spawn point
    assert isinstance(c.yaw, float)
    assert c.mode in ("wire", "flat", "zbuf")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python test_client.py`
Expected: FAIL — `AttributeError: module 'client' has no attribute 'Client'`.

- [ ] **Step 3: Implement `Client.__init__` / `resize` by relocating engine setup**

In `client.py`, add the module imports currently used by those parts of `main.py` (`math`, `from quake.pak import Pak`, `Bsp`, `Renderer`, `angle_vectors`, `lightstyle_values`, `Physics`, `VIEW_HEIGHT`, `MAXSPEED`, `Progs`, `Server`, `anglemod`, `Mdl`, `EF_ROTATE`, `PickupModel`, `from quake import snd`) and the gameplay constants from `main.py:32-62` that are not Tk-specific (`PAK_PATH`, `SV_MAXFRAME`, `NOCLIP_SPEED`, `LOOK_SENS`, `YAW_SPEED`, `PARTICLE_*`, `CENTER_MSG_TIME`, `CL_BOB`, `CL_BOBCYCLE`, `CL_BOBUP`). Move the helper functions `view_origins` and `spin_yaw` verbatim into `client.py`.

Write `Client.__init__(self, mapname)` containing everything from `App.__init__` (`main.py:131-232`) **except**: the tkinter window/canvas/item-pool creation (`main.py:151-191`), the `self.gdi/self.rawmouse/self.gdi_present/self._overlays_visible` block, the `self._diag*` block, `self._bind()`, `self.canvas.focus_set()`, the win32 `win_ui` setup, and `self.root.after(...)`. Keep: pak/palette load, mixer + audio backend selection (it has no window — keep as-is), the input-state primitives that are game state (`self.keys` is frontend-owned — drop it from Client; `self.mouselook`/`self._last_mouse` are frontend-owned — drop), `self.fire_mouse/fire_key/attacking` (combine into a single `self.fire` set by input), `self.pending_impulse`, `self.intermission`. Add `self.mode = "zbuf" if self.zbuf else "flat" if self.flat else "wire"` derived from existing `self.flat`/`self.zbuf`/`self.textured` flags (keep those flags). Replace `self._load_map` failure `sys.exit` with raising `ValueError`.

Move `_load_map` (`main.py:245-328`) into `Client`, dropping the Tk bits: `self.root.title(...)` (`:259`) and the `self.canvas.winfo_width/height` resize block (`:323-327`) — instead store `self._view_wh = (0, 0)` and let `resize` drive the renderer.

Add:

```python
    def resize(self, w, h):
        self._view_wh = (w, h)
        self.rend.resize(w, h)
```

Move `_change_level`, `_move`, `_wishmove`, `_calc_bob`, `_alias_ents`, `_bsp_ents`, `_sync_from_player`, `_view_model` into `Client` unchanged (they reference `self.sv/self.phys/self.pos/self.yaw/...`, all now on `Client`).

- [ ] **Step 4: Run it to verify it passes**

Run: `python test_client.py`
Expected: `OK` (all three tests).

- [ ] **Step 5: Commit**

```bash
git add client.py test_client.py
git commit -m "Move engine boot + movement/level logic into Client"
```

### Task 1.3: `Client.frame()` — tick body minus drawing, returns `RenderFrame`

**Files:**
- Modify: `client.py`
- Reference: `App.tick` (`main.py:488-761`) and the HUD-string block (`main.py:656-722`).
- Test: `test_client.py`

- [ ] **Step 1: Write the failing test for `frame()`**

Add to `test_client.py` (and to `__main__`):

```python
def test_frame_returns_zbuf_renderframe_sized_to_viewport():
    c = client.Client("e1m1")
    c.resize(800, 600)
    c.mode = "zbuf"
    rf = c.frame(0.016, client.InputState())
    assert rf.mode == "zbuf"
    fb, w, h = rf.framebuffer
    assert w == 800 // 4 and h == 600 // 4        # ZBUF_SCALE == 4
    assert len(fb) == w * h * 3                    # packed RGB
    assert any("fps" in o[2] for o in rf.overlays) # HUD line present


def test_frame_forward_input_moves_player():
    c = client.Client("e1m1")
    c.resize(320, 240)
    c.noclip = True                                # fly so movement is unconstrained
    start = list(c.pos)
    for _ in range(5):
        c.frame(0.05, client.InputState(move_forward=1.0))
    assert c.pos != start
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python test_client.py`
Expected: FAIL — `AttributeError: 'Client' object has no attribute 'frame'`.

- [ ] **Step 3: Implement `Client.frame(dt, inp)`**

Port `App.tick` (`main.py:488-761`) into `Client.frame(self, dt, inp)`. Transform rules:
- Drop the timing/`after`/`fps-EMA`/`_diag` scaffolding; take `dt` as a parameter. Keep an `fps` EMA off `dt` for the HUD (`self.fps = 0.9*self.fps + 0.1*(1/dt) if dt>0 else self.fps`).
- Replace `self._apply_rawlook()` and the keyboard movement reads with: apply `inp` — `self.yaw -= inp.look_dx * LOOK_SENS; self.pitch = clamp(self.pitch + inp.look_dy * LOOK_SENS)`; keyboard turn `self.yaw -= inp.turn * YAW_SPEED * dt`; set `self.fire = inp.fire`; `self.pending_impulse = inp.impulse or self.pending_impulse`; apply `inp.commands` (toggle `self.noclip/self.flat/self.zbuf/self.textured` and recompute `self.mode`).
- `_move` currently reads `self.keys`; change `_wishmove` (moved in 1.2) to take `inp` and read `inp.move_forward/move_strafe/move_up/turn/run` instead of `self.keys`. Update the `_move` call to pass `inp`.
- Replace the three drawing branches (`self._draw_fb`/`self._draw_polys`/`self._draw`, `main.py:630-652`) with building the payload: `zbuf` → `framebuffer = self.rend.render_zbuffer(...)[0]`; `flat` → `polys = self.rend.render_shaded(...)`; `wire` → `segs = self.rend.render(...)`. Keep the `leaf`/entity-gathering calls as today.
- Replace `self._draw_particles(eye)` with computing the particle sprite list (the projection math currently in `App._draw_particles`) and storing it in `RenderFrame.particles` as `(x, y, half)` tuples; the frontend draws them.
- Replace the HUD `itemconfig`/`coords` block (`main.py:656-722`) with building `overlays` as `(x,y,text,rgb,anchor)` tuples: HUD line at `(8,8,...,'nw')`, status bar at `(10,h-8,...,'sw')`, centerprint at `(w//2,h//3,...,'center')` when active, and `crosshair=(w//2,h//2)`. Reuse the exact strings/colors from `main.py:671-690` and the `mode` label logic.
- Return `RenderFrame(mode=self.mode, segs=..., polys=..., framebuffer=..., particles=..., overlays=..., crosshair=...)` with only the active mode's payload set.

- [ ] **Step 4: Run it to verify it passes**

Run: `python test_client.py`
Expected: `OK` (all five tests).

- [ ] **Step 5: Run the full suite to confirm no engine regression**

Run: `for t in test_*.py; do printf '%s: ' "$t"; python "$t" 2>&1 | tail -1; done`
Expected: every line prints `OK`.

- [ ] **Step 6: Commit**

```bash
git add client.py test_client.py
git commit -m "Add Client.frame(): produce a RenderFrame per tick"
```

### Task 1.4: Reduce `main.py` `App` to a thin tkinter frontend over `Client`

**Files:**
- Modify: `main.py`
- Reference: the just-created `Client`; existing draw pools `App._draw` / `_draw_polys` / `_draw_particles` / `_draw_fb` and the pool setup (`main.py:151-191`).

- [ ] **Step 1: Rewrite `App.__init__` to own only Tk + a `Client`**

`App.__init__(self, mapname)` keeps the tkinter window/canvas, the line/poly/particle item pools, HUD/crosshair/center/status text items, and `self.fb_item`/`self.fb_photo` (`main.py:151-191`). Replace all engine/state setup with `self.client = Client(mapname)`. Keep frontend input state: `self.keys=set()`, `self.mouselook=False`, `self._last_mouse=None`, `self.last_t=time.perf_counter()`. **Delete** `self.gdi/self.rawmouse/self.gdi_present/self._overlays_visible`, the `self._diag*` block, and the win32 `win_ui` setup block. Call `self._bind()`, `self.canvas.focus_set()`, `self.root.after(16, self.tick)`.

- [ ] **Step 2: Build `InputState` from Tk keys + warp mouselook**

Add `App._input(self) -> client.InputState` that maps `self.keys` to axes (forward = W/up−S/down, strafe = D−A, up = space, turn = right−left arrows, run = shift), `fire = self.fire_mouse or self.fire_key`, `impulse = self._pending` (consume once), `commands` from any toggle keypresses queued this frame, and `look_dx/look_dy` from the warp-delta accumulator (see Step 4). Restore the warp mouselook: keep `look_delta`, `_warp_center`, `_motion`, `_set_mouselook` from the **pre-raw** design — `_motion` accumulates `(dx,dy)` into `self._look_accum` (init `(0,0)`); `_set_mouselook(True)` does `cursor="none"` + `_warp_center`; no `rawmouse`. Remove the raw-input branches added earlier in `_motion`/`_set_mouselook`/`_apply_rawlook` (delete `_apply_rawlook`).

- [ ] **Step 3: Rewrite `App.tick` to drive `Client` and draw the `RenderFrame`**

```python
    def tick(self):
        now = time.perf_counter()
        dt = now - self.last_t
        self.last_t = now
        self.client.resize(self.canvas.winfo_width(), self.canvas.winfo_height())
        rf = self.client.frame(dt, self._input())
        self._draw_frame(rf)
        work_ms = (time.perf_counter() - now) * 1000
        self.root.after(max(1, int(16 - work_ms)), self.tick)
```

Add `_draw_frame(self, rf)` that dispatches on `rf.mode`: `wire` → existing `_draw(rf.segs)` and park poly pool + hide `fb_item`; `flat` → existing `_draw_polys(rf.polys)` and park line pool + hide `fb_item`; `zbuf` → `_draw_fb(rf.framebuffer)` and park both pools + show `fb_item`. Then draw `rf.particles` via the existing particle rect pool, set the HUD/crosshair/center/status text items from `rf.overlays`/`rf.crosshair` (map the `nw/center/sw` anchors to the existing item coords), and `tag_raise` them. The `_draw*` pool helpers stay as-is; they now take data from `rf` instead of computing it.

- [ ] **Step 4: Keep mode toggles producing `commands`**

In `_keydown`, change `N/F/Z/T` and `1-8` to enqueue into `self._cmd_queue`/`self._pending` consumed by `_input()` (instead of mutating engine state directly). `Tab`/`Esc` stay frontend-only (`_set_mouselook`, quit). Remove the `G` key and the `z`-handler `fb_item`/`gdi_present` logic added earlier — `_draw_frame` owns `fb_item` visibility now.

- [ ] **Step 5: Run the full suite + play-test**

Run: `for t in test_*.py; do printf '%s: ' "$t"; python "$t" 2>&1 | tail -1; done`
Expected: all `OK`.
Then manually: `python main.py e1m1` on this machine — walk, look (mouse, Tab to capture), toggle N/F/Z/T, fire. Confirm it plays exactly as before the gdi work. (On macOS this is the unchanged warp path.)

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "Reduce main.py App to a thin tkinter frontend over Client"
```

---

## STAGE 2 — `win_gdi.py`: full game via `Client`, textured + raw mouselook

Goal: a Windows frontend that plays the real game (not noclip flight) in textured mode with raw mouselook, drawing through `Client`/`RenderFrame`. Wire/flat temporarily render as textured.

### Task 2.1: `win_gdi.py` window/loop/input shell driving `Client`

**Files:**
- Create: `win_gdi.py`
- Reference: `spike_gdi.py` (window class, `PeekMessage` loop, raw grab), `client.Client`, `win_ui.GdiBlitter`.

- [ ] **Step 1: Build the frontend from the spike**

Create `win_gdi.py` exposing a module-level `run(mapname)` entry point (called by `main.py` in Stage 4). Reuse `spike_gdi.SpikeWindow`'s window creation, `_grab`, `_proc`, `_read_raw`, `pump`, `read_mouse`, `client_size`, `shutdown` (copy `SpikeWindow` into `win_gdi.py` as the window class; the spike is deleted in Stage 4). `run(mapname)` builds the window + `Client` + `GdiBlitter` and runs the loop:
- construct `Client(mapname)`, `GdiBlitter(hwnd)`, `client.resize(*client_size())`;
- each iteration: `pump()`; build an `InputState` from the held VK set + `read_mouse()` (look) + raw `left_down` (fire) + edge-detected toggles; `rf = client.frame(dt, inp)`; draw `rf`.
- For Stage 2, draw only the textured path: `blitter.present(rf.framebuffer[0], *rf.framebuffer[1:], cw, ch, texts=rf.overlays + [(\*rf.crosshair, '+', (0,255,102), 'center')])`. If `rf.mode != 'zbuf'`, force `inp.commands` to include `'zbuf'` once at startup so the world renders (wire/flat handled in Stage 3).

- [ ] **Step 2: Map VK codes to `InputState`**

Map: W/A/S/D → forward/strafe; Space → up; Shift (`0x10`) → run; arrows (`0x25/0x27/0x26/0x28`) → turn/forward; Ctrl (`0x11`) → fire (OR with raw `left_down`); `1`–`8` (`0x31`–`0x38`) → impulse; `N/F/Z/T` (`0x4E/0x46/0x5A/0x54`) → edge-detected `commands`; Esc → quit; Tab → toggle mouselook (grab on/off). Edge-detect toggles by diffing the held set against the previous frame's set.

- [ ] **Step 3: Smoke it**

Create `smoke_win_gdi.py` (mirror `smoke_spike.py`): construct the frontend with `Client("e1m1")`, `pump()` + one `client.frame()` + draw, a few times, `shutdown()`, print `OK`. Run: `python smoke_win_gdi.py` → `OK`.

- [ ] **Step 4: Play-test + commit**

Run `python win_gdi.py e1m1`: confirm textured world, smooth raw mouselook (no spin — `raw/s` parity with the spike), WASD movement, fire. Then:

```bash
git add win_gdi.py smoke_win_gdi.py
git commit -m "Add gdi32 Windows frontend (textured, raw mouselook) over Client"
```

---

## STAGE 3 — GDI vector drawing (wireframe / flat / particles)

Goal: all three render modes work in `win_gdi`, drawn with GDI primitives, double-buffered to avoid flicker.

### Task 3.1: Add vector/particle drawing to `GdiBlitter`

**Files:**
- Modify: `win_ui.py` (`GdiBlitter`)
- Reference: existing `GdiBlitter.present` (sets up the DC), Win32 `CreateCompatibleDC`/`CreateCompatibleBitmap`/`BitBlt`, `CreatePen`/`Polyline`, `CreateSolidBrush`/`Polygon`, `FillRect`.

- [ ] **Step 1: Memory-DC double buffer**

Add a `present_vector(self, draw_calls, dst_w, dst_h, texts)` path: create/cache a memory DC + compatible bitmap sized to the window, clear it (black `FillRect`), run the draw calls into it, then `BitBlt` to the window DC in one shot (no flicker). Define ctypes signatures for the gdi32 functions above next to the existing ones.

- [ ] **Step 2: Wire segments via `Polyline`**

Add `draw_segs(memdc, segs)`: one `CreatePen(PS_SOLID, 1, colorref(LINE_COLOR))`, `SelectObject`, then per segment `MoveToEx`+`LineTo` (or batch contiguous runs into `Polyline`). Clean up the pen (`DeleteObject`).

- [ ] **Step 3: Flat polygons via `Polygon`**

Add `draw_polys(memdc, polys)`: per `(points, color)` create a solid brush, `SelectObject`, `Polygon(points)`, delete the brush. Points are the `(x,y)` pairs the renderer already produces.

- [ ] **Step 4: Particles via `FillRect`**

Add `draw_particles(memdc, particles)`: per `(x, y, half)` a small `FillRect` with a white brush (matches the Tk particle squares).

- [ ] **Step 5: Smoke the vector path**

Extend `smoke_win_gdi.py` to also render one `wire` and one `flat` frame (force the mode) and present them. Run → `OK`.

- [ ] **Step 6: Commit**

```bash
git add win_ui.py smoke_win_gdi.py
git commit -m "Add GDI vector/particle drawing to GdiBlitter"
```

### Task 3.2: Use the vector path in `win_gdi` per mode

**Files:**
- Modify: `win_gdi.py`

- [ ] **Step 1: Dispatch on `rf.mode`**

Replace the Stage-2 force-`zbuf` hack with: `zbuf` → `present` (framebuffer); `wire` → `present_vector` drawing `rf.segs`; `flat` → `present_vector` drawing `rf.polys`. Draw `rf.particles` and `rf.overlays` + crosshair in every mode (particles into the memory DC for vector modes; over the framebuffer for zbuf — extend `present` to take particles too, or draw them as overlays).

- [ ] **Step 2: Play-test all three modes + commit**

Run `python win_gdi.py e1m1`, toggle Z/F to cycle textured/flat/wire — all render, mouselook stays smooth. Then:

```bash
git add win_gdi.py
git commit -m "Render all three modes in the gdi32 frontend"
```

---

## STAGE 4 — Entry point + cleanup

### Task 4.1: Platform dispatch and retire the spike

**Files:**
- Modify: `main.py` (`__main__`)
- Delete: `spike_gdi.py`, `smoke_spike.py`
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Dispatch by platform with a `--tk` override**

Change `main.py`'s `__main__`: parse args for `--tk`; if `sys.platform == "win32"` and not `--tk`, `import win_gdi; win_gdi.run(mapname)`; else `App(mapname).run()`. Keep `App` usable on Windows via `--tk` (the warp fallback).

- [ ] **Step 2: Delete the spike**

```bash
git rm spike_gdi.py smoke_spike.py
```

- [ ] **Step 3: Update docs**

In `README.md` and `CLAUDE.md`, document the two frontends + `Client` core and the `quake/` engine boundary: engine in `quake/`; `client.py` = UI-agnostic core; `main.py` = tkinter frontend (all platforms / `--tk`); `win_gdi.py` = default Windows frontend (own loop, raw mouselook, GDI drawing); `win_ui.py` = GDI/raw ctypes helpers. Note the `win_gdi` controls.

- [ ] **Step 4: Full suite + both frontends + commit**

Run: `for t in test_*.py; do printf '%s: ' "$t"; python "$t" 2>&1 | tail -1; done` → all `OK`.
Run `python main.py e1m1` (→ gdi32 on Windows) and `python main.py --tk e1m1` (→ tkinter): both play. Then:

```bash
git add -A
git commit -m "Default Windows to the gdi32 frontend; retire the spike; update docs"
```

---

## Self-Review notes

- **Spec coverage:** full-parity modes/HUD/particles/etc → Stages 1–3 (`Client.frame` builds all; both frontends draw all). Shared core + two contracts → Task 1.1–1.4. Each frontend owns its loop → `App.tick` (after) / `win_gdi` (PeekMessage). Staging + testing → as specced. Entry point + retire spike → Stage 4.
- **Contracts are concrete code** (Task 1.1), referenced by name in every later task.
- **Relocations** cite exact `main.py` line ranges and the specific Tk-decoupling edits, not "similar to".
- **Out of scope (per spec):** textured render perf (~20fps) — untouched.
