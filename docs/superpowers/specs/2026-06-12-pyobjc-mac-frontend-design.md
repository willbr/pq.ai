# PyObjC Cocoa frontend for macOS — design

Date: 2026-06-12. Status: approved.

## Goal

Replace tkinter as the default macOS frontend with a native Cocoa frontend built on
PyObjC, mirroring what `win_gdi.py` did for Windows: an owned drain-then-step frame
loop, a direct CoreGraphics present path, and true relative mouse input. tkinter
remains the `--tk` fallback everywhere and the default on Linux.

Decisions made during brainstorming:

- **Dependency policy: PyObjC is required on macOS.** If it fails to import, exit
  with a clear `pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz` message
  and a note that `--tk` runs without it. No silent fallback.
- **Scope: full parity with win_gdi** — all render modes (zbuf/wire/wire_hidden/flat),
  particles, HUD/statusbar/crosshair/center text, drop-down console, overlay menu,
  profiler HUD.
- **Present path: NSView `drawRect:` + CoreGraphics drawing**, driven synchronously
  by our own loop (`view.display()` per frame). Not CALayer.contents, not
  AppKit-paced NSTimer.

## Files

- `mac_cocoa.py` (new, repo root) — the frontend. NSApplication/NSWindow setup, a
  `GameView(NSView)` subclass, the owned frame loop, InputState building,
  `run(mapname)`. The PyObjC twin of `win_gdi.py`. Imports `client`, `quake.perf`,
  `quake.console`, `mac_ui`.
- `mac_ui.py` (new, repo root) — pure/testable helpers, the twin of `win_ui.py`:
  - macOS virtual keycode → key-name mapping (hardware keycodes from `keyDown:`).
  - framebuffer → RGBA expansion (reuse `main.py`'s three-pass `bytes.translate`
    trick plus a constant alpha plane).
  - CG drawing helpers for the console panel, overlay menu, and text overlays,
    each taking an explicit CGContext (testable against a headless
    `CGBitmapContext`).
- `tests/test_mac_ui.py` (new) — unit tests for the pure helpers (keycode map,
  RGBA expansion); follows the `tests/test_win_ui.py` pattern.
- `main.py` — `select_frontend` gains a `"cocoa"` result: darwin defaults to
  cocoa, win32 to gdi, `--tk` (or any other platform) to tk. `__main__` dispatches
  to `mac_cocoa.run(mapname)`; an ImportError for PyObjC produces the install
  message above. The warp-mouselook code stays (it is the Tk fallback's path).
- `client.py`, `quake/`, `mac.py` (CoreAudio), `win_gdi.py`, `win_ui.py` —
  untouched.
- `README.md` + `CLAUDE.md` — identity line becomes: pure stdlib; tkinter
  (fallback UI) and PyObjC (macOS UI) are the only non-stdlib dependencies.

## Frame loop (mac_cocoa.run)

Same structure as `win_gdi.run`:

```
while running:
    drain: nextEventMatchingMask:NSAnyEventMask untilDate:[NSDate distantPast]
           inMode:NSDefaultRunLoopMode dequeue:YES  → NSApp.sendEvent_(e)  (repeat until None)
    rf = client.frame(dt, build_input())
    view.display()                  # synchronous drawRect:
    sleep(remainder of ~16ms)
```

Events are forwarded through `NSApp.sendEvent_` so window dragging, the app menu,
and Cmd-Q keep working; `GameView`'s responder methods capture game input as the
events route through it. Raw deltas accumulate in the view between frames and are
read once per frame — input bursts coalesce by construction (the win_gdi property).

## Input

- `keyDown_`/`keyUp_` maintain a held-keys set keyed by names from
  `mac_ui.KEYCODE_NAMES` (hardware keycodes, layout-independent for WASD);
  `flagsChanged_` tracks Shift (run) and Ctrl (fire).
- One-shot keys mirror win_gdi: F1/backtick console toggle, Esc menu, Tab
  mouselook, N/F/Z/T/P command queue, 1–8 impulses.
- While the console or menu is active, keys route to
  `Console.key_*` / `Menu.key_*` (port of `win_gdi._console_key` / `_menu_key`,
  using `charactersIgnoringModifiers` for printables) and the game receives a
  do-nothing InputState.
- Mouselook: click-to-grab. Grabbed = `NSCursor.hide()` +
  `CGAssociateMouseAndMouseCursorPosition(False)`; mouse-moved/dragged events'
  `deltaX`/`deltaY` accumulate (true relative input — no warp, no recenter, no
  `look_delta` guard). Ungrabbed = cursor visible, association restored, clicks
  work normally. Window needs `setAcceptsMouseMovedEvents_(True)`.

## Drawing (GameView.drawRect_)

`isFlipped` returns True so view coordinates are y-down, matching RenderFrame.
All drawing uses the current CGContext:

- **zbuf**: expand the 8-bit framebuffer to RGBA via `mac_ui`, wrap with
  `CGDataProviderCreateWithCFData` + `CGImageCreate`, draw with
  `kCGInterpolationNone` into the largest aspect-correct rect
  (`win_ui.letterbox_rect` — a pure, platform-free helper imported directly),
  with black bars and particle remapping exactly as `win_ui.GdiBlitter.present`
  does. `fb_fit`'s integer-only scaling is a Tk-only workaround and is not used.
  Palette LUTs cached against `palette_version` as in `main.py`.
- **wire / wire_hidden**: `CGContextStrokeLineSegments` for segs; filled+stroked
  paths back-to-front for hidden-line.
- **flat**: filled CGPaths back-to-front.
- **particles**: filled rects.
- **text** (HUD, statusbar, center text, crosshair, console, menu, profiler
  bars): AppKit string drawing (`NSString drawAtPoint:withAttributes:`, the
  high-level wrapper over CoreText — flipped-view aware) with Menlo (CLAUDE.md
  already records that Menlo carries the 1/8-block glyphs the profiler bars
  need). Console/menu panel layout ported from
  `win_ui.draw_console`/`draw_menu`.

Retina: drawing is in points; the fb image upscale stays chunky because
interpolation is off.

## Lifecycle

- `NSApplication.sharedApplication()`, `setActivationPolicy_(Regular)`, minimal
  app menu (Quit), `makeKeyAndOrderFront_`, `activateIgnoringOtherApps_(True)` —
  without the activation dance there is no key window.
- Window close (delegate `windowWillClose_`) and Cmd-Q set `running = False`.
- `client.quit_requested` (console `quit`, menu Quit) → `client.shutdown()` →
  loop exit. Same ordering as both existing frontends.
- Resize: read view bounds each frame into `client.resize` (the menu's video-mode
  resolution switch already flows through the Client).
- Title: retitle on `client.mapname` change (changelevel), as in both frontends.

## Error handling

- PyObjC missing on darwin → `sys.exit` with the pip command and the `--tk` hint.
- Everything else (no audio device, missing pak) is unchanged Client behaviour.

## Testing

- `tests/test_mac_ui.py`: pure-helper unit tests (keycode map completeness/shape,
  RGBA expansion correctness against a hand-computed palette) — runs headless on
  any platform that has the module importable; PyObjC-dependent pieces guarded
  with a skip when PyObjC is absent.
- The frontend itself is verified manually (`python main.py e1m1` on macOS), the
  `smoke_win_gdi.py` precedent. No CI window creation.
- Existing tests must stay green: `PQ_AUDIO=0; for t in tests/test_*.py; do python "$t"; done`.
