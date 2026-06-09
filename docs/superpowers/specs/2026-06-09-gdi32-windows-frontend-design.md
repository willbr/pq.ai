# gdi32 Windows frontend — design

**Date:** 2026-06-09
**Status:** approved (design)

## Problem

The Windows mouselook bolted onto the tkinter front-end is unusable: a hard mouse
swing spins the view for 10+ seconds and freezes WASD. Diagnosis (PQ_DIAG run +
`spike_gdi.py`) traced this to ownership: **tkinter owns the Win32 message pump**,
and the ~13–50ms software render blocks it ~65% of every frame, so raw `WM_INPUT`
events queue faster than Tk drains them. The backlog spins the view while it drains
and starves the keyboard. The `spike_gdi.py` proof — a pure-gdi32 window with a
classic `PeekMessage` loop that drains *all* input each frame — confirmed the fix:
`raw/s` drops to 0 the instant motion stops, no spin, WASD stays live.

This spec turns that proof into a full-parity Windows frontend without forking the
game logic, by extracting a shared, UI-agnostic core.

## Goals

- Full feature parity with today's `main.py` App on the gdi32 frontend: all three
  render modes (wireframe / flat / textured), HUD + status bar + centerprint,
  particles, weapon view-model, death cam, intermission, changelevel, sound
  listener, fire (mouse + Ctrl), weapon-select/impulse, noclip, every keybind.
- One copy of the game logic, shared by both frontends.
- Each frontend owns its own native event loop (this is the crux of the fix).

## Non-goals

- Rendering performance (textured ~20fps is the pure-Python rasteriser's cost, a
  separate axis — not addressed here).
- Linux/macOS gdi equivalent (Tk stays the cross-platform frontend).

## Architecture (Approach A: Client produces frames)

```
quake/            platform-agnostic engine (unchanged)
client.py         Client core: engine stack + all camera/player/game state + frame()
main.py           tkinter frontend (all platforms): after() loop, Canvas/PhotoImage
win_gdi.py        Windows frontend: PeekMessage loop, raw input, GdiBlitter drawing
win_ui.py         GDI/raw ctypes helpers (GdiBlitter, RAWINPUT, etc.) — reused
mac.py / win.py   audio backends (unchanged)
```

The only things both frontends must agree on are two data contracts. Everything
else (window creation, cursor grab, mouselook toggle, quit, the loop) is
frontend-private.

### Contract: `InputState`

A frontend fills this each frame from native events; `Client` only reads it.

| Field | Type | Meaning |
|---|---|---|
| `move_forward` | float −1..1 | W/S or up/down arrows |
| `move_strafe` | float −1..1 | A/D |
| `move_up` | float −1..1 | space (jump when walking / ascend when noclip) |
| `turn` | float −1..1 | keyboard yaw (left/right arrows) |
| `look_dx`, `look_dy` | float | mouse counts since last frame; sent only while mouselook is engaged |
| `run` | bool | shift (faster) |
| `fire` | bool | mouse button or Ctrl |
| `impulse` | int | weapon select 1–8, 0 = none |
| `commands` | set[str] | one-shot edge-triggered toggles this frame: subset of `{noclip, flat, zbuf, texture}` |

Mouselook on/off, the cursor grab, and quit are deliberately *not* here — they are
platform-specific and frontend-owned. The frontend simply withholds `look_dx/dy`
when mouselook is off.

### Contract: `RenderFrame`

What `Client.frame()` returns; the frontend draws it.

- `mode` ∈ `{wire, flat, zbuf}`
- mode payload: `segs` (list of line segments) | `polys` (list of (points, color)) |
  `framebuffer` (bytes, w, h)
- always present: `particles` (point sprites), `overlays` (list of
  `(x, y, text, rgb, anchor)` with anchor ∈ `{nw, center, sw}`), `crosshair` (x, y)

The `overlays` anchor model matches `GdiBlitter.present`'s existing text contract,
so the GDI frontend draws overlays directly and the Tk frontend maps them to Canvas
text items.

### `Client` core (`client.py`)

Holds the engine stack (Pak/Bsp/Renderer/Physics/Server) and all camera/player/game
state — everything in today's `App` except tkinter. Methods:

- `Client(mapname)` — boot the stack and load the level.
- `resize(w, h)` — frontend reports viewport size; forwards to the renderer.
- `frame(dt, input) -> RenderFrame` — today's `tick()` body minus drawing: apply
  input → run one server frame → view calc → gather brush/alias/bsp entities → call
  the current mode's renderer → build HUD strings → return a `RenderFrame`.
- level loading / changelevel / death-cam / intermission / sound-listener move here
  verbatim from `App`.

`Client` owns render mode (`wire`/`flat`/`zbuf` + textured flag) and noclip, toggled
via `input.commands`.

### Frontends

- **`main.py` (tkinter, all platforms):** a *clean* Tk frontend — `after()` loop, Tk
  events → `InputState`, draws `RenderFrame` via Canvas items + `PhotoImage`,
  mouselook via the existing **warp** path. The broken raw-mouselook, GDI-present,
  and `PQ_DIAG` instrumentation currently bolted into `App` are removed (raw/GDI move
  to `win_gdi.py`).
- **`win_gdi.py` (Windows):** `spike_gdi.py` grown up — owns the `PeekMessage`
  drain-all loop, raw mouselook + cursor grab, drives `Client.frame()`, and draws via
  `GdiBlitter`: `StretchDIBits` framebuffer, `Polyline` for wire, `Polygon` for flat,
  `FillRect` particles, `TextOut` overlays.

## Staging

Each stage leaves the tree working and is independently verifiable.

1. **Extract `Client`; refactor `main.py` onto it — zero behavior change, all
   platforms stay on Tk.** Verify: existing `test_*.py` pass, a new headless
   `Client.frame()` test passes, and the game plays identically. Removes the broken
   raw/GDI/diag code from `App` (Tk reverts to the warp mouselook everywhere).
2. **`win_gdi.py`: full game via `Client`, textured mode + raw mouselook** (reuse the
   spike's window/loop/grab + `GdiBlitter`). Wire/flat temporarily fall back to
   textured.
3. **GDI vector drawing** — port wire (`Polyline`), flat (`Polygon`), and particles
   (`FillRect`) so all three modes work in `win_gdi`.
4. **Entry point** — on Windows launch `win_gdi` by default, `--tk` forces tkinter;
   retire `spike_gdi.py`/`smoke_spike.py`; update README + CLAUDE.md.

## Testing

- Existing `test_*.py` already boot the full engine, so they exercise `Client`'s
  dependencies; they must stay green through every stage.
- New headless test: construct `Client`, feed a few `InputState`s, assert a sane
  `RenderFrame` (correct `mode`, non-empty `overlays`, framebuffer dimensions match
  the requested viewport).
- GDI drawing stays smoke-tested (`smoke_*.py`) since it needs a live window.
- Pure helpers (`win_ui` channel swap / DIB packing / raw delta / button state)
  remain unit-tested in `test_win_ui.py`.

## Risks

- **Big internal refactor (Stage 1).** Mitigation: it's behavior-preserving and
  guarded by the existing suite + a new `Client` test; the game must play identically
  before moving on.
- **GDI vector drawing fidelity (Stage 3).** Wire/flat currently lean on Tk Canvas
  conveniences (retained items, parking). GDI is immediate-mode; mitigation is
  double-buffering into a memory DC to avoid flicker, validated by eye.
- **Two frontends drifting.** Mitigation: the contracts are the only shared surface;
  game logic lives solely in `Client`, so neither frontend can fork it.
