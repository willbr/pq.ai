# Video Options Menu — Design

**Date:** 2026-06-09
**Status:** Approved design, pending implementation plan
**Backlog:** advances ideas.md #6 (Menu) with a focused video-options slice; relates to #3 (dynamic resolution).

## Goal

Add an Escape-opened, keyboard-driven in-game menu whose first (and currently only)
section is **Video Options → Resolution**, letting the player choose the textured
software rasteriser's internal render resolution from a fixed set:

- **240x160**
- **320x240** (default)
- **640x480**

The chosen resolution is the exact size of the textured (z-buffer) framebuffer; it is
stretched to fill the window by the existing `StretchDIBits` present path, regardless of
window size. This directly controls the per-pixel Python rasteriser cost (the textured-mode
bottleneck).

## Scope decisions (from brainstorming)

- **Render-resolution semantics:** a *fixed internal render buffer*, stretched to fill the
  window — not a window resize.
- **Render modes affected:** **textured (z-buffer) only.** Wireframe and flat-shaded modes
  are vector modes drawn straight to the window by GDI and keep rendering at window size;
  they are untouched.
- **Menu UI:** an Escape overlay menu, navigated by keyboard only (arrows + Enter, Esc to
  back out / close). Today Escape quits the game directly; that moves into the menu as a
  **Quit** item. The window close button [X] still quits.
- **Frontend:** **gdi32 only** (`win_gdi.py`). The tkinter frontend (`main.py`) is out of
  scope for this slice.
- **Default resolution:** 320x240.

## Architecture

The design mirrors the existing **console** split exactly: a pure, UI-agnostic state machine
in the `quake/` package (like `quake/console.py`), owned by the UI-agnostic `Client`, whose
view is exposed on `RenderFrame`; the gdi32 frontend routes native keys into it and draws it
via a `GdiBlitter` method. This keeps the menu logic testable with the existing `_boot()`
pattern and consistent with the codebase's "pure mechanism + frontend draws" discipline.

### Components

**1. Fixed render resolution — `quake/render.py` (`Renderer`)**

- New attribute `self.video_res = None`. `None` preserves today's behaviour exactly
  (textured buffer = `width // zbuf_scale`), so the bare-`Renderer` defaults and the
  existing `test_renderer_zbuf_scale_is_live` stay green.
- `_setup_zbuf()`: if `self.video_res` is set, `zw, zh = self.video_res`; otherwise the
  current `width // zbuf_scale` derivation.
- `resize(w, h)` already calls `_setup_zbuf()`, so a window resize keeps a fixed res.
- No new present path needed: `present()` already `StretchDIBits` the `zw × zh` buffer to
  the window client size.

**2. Client video state + API — `client.py` (`Client`)**

- `VIDEO_MODES`: an ordered list of `(label, (w, h) | None)`. Concretely:
  `[("Auto", None), ("240x160", (240, 160)), ("320x240", (320, 240)), ("640x480", (640, 480))]`.
  See the "zbuf_scale reconciliation" note below for why **Auto** is retained.
- `self.video_res` persisted across maps like `self._zbuf_scale`, initialised to the
  **320x240** entry, and re-applied to the freshly built `Renderer` in `_load_map`
  (set `self.rend.video_res = self.video_res` before/with the existing
  `self.rend.zbuf_scale = self._zbuf_scale` line, then the renderer's `resize` rebuilds the
  buffer).
- `set_video_res(wh)`: store on `self` and on `self.rend`, then re-run the renderer's zbuf
  setup (via `resize(*self._view_wh)` when a size is known, matching `_on_zbuf_scale`).

**3. Menu model — `quake/menu.py` (new, pure)**

Same import discipline as `quake/console.py` (stdlib only, no ctypes/GDI, single-thread).

- `Menu`: `active` flag, an ordered list of items, a `selected` index, and key methods
  `key_up`, `key_down`, `key_left`, `key_right`, `key_enter`, `key_escape`.
- Two item kinds:
  - **Choice item** (Resolution): holds an ordered list of options, a current index, and an
    `on_select(value)` callback. `key_left`/`key_right` (and `key_enter`) cycle the option
    and fire `on_select`.
  - **Action item** (Back, Quit): holds an `on_activate()` callback fired by `key_enter`.
- `view()` returns a small immutable structure the frontend draws, e.g.
  `(title, [(label, value_str, is_selected), ...])`.
- `Client` builds the `Menu` with closures bound to its own methods — `Resolution` →
  `set_video_res`, `Back` → close the menu, `Quit` → `self.quit_requested = True` — exactly
  as it registers console commands in `_register_console`.

**4. RenderFrame + frontend wiring**

- `client.py` `RenderFrame`: new field `menu = None`; set to `self.menu.view()` when the
  menu is active (parallels the existing `console` field). `Client.frame` toggles/forwards
  nothing itself — input routing lives in the frontend, matching how the console is driven.
- `win_gdi.py` `GameWindow`:
  - Hold a reference to the menu (wired in `run()` like `self.console = client.con`).
  - In `_proc`, **Escape** toggles the menu when the console is *not* active (console keeps
    its current Escape = close behaviour and is checked first, as F1 is today). Opening the
    menu clears held keys and ungrabs the mouse (same as `_toggle_console`).
  - While the menu is active, route arrows/Enter/Esc to the menu's key methods and **swallow
    all other game input** (mirrors the `self.console.active` branch in `_proc` and
    `build_input`, which returns a do-nothing `InputState`).
  - Quit no longer happens on Escape; it happens via the menu's **Quit** item setting
    `client.quit_requested`, which `run()` already honours. The [X] close path is unchanged.
- `win_ui.py` `GdiBlitter`: new `draw_menu(view, dst_w, dst_h)` drawing a centered panel
  with the title and items, highlighting the selected row. Mirrors `draw_console`
  (same font handling); called from `run()` when `rf.menu is not None`.

### zbuf_scale reconciliation (flagged for review)

Both `zbuf_scale` and a fixed `video_res` size the *same* textured framebuffer, so they must
be reconciled:

- When `video_res` is a fixed mode, it is **authoritative** and `zbuf_scale` does not affect
  the buffer size.
- The **Auto** mode (`video_res = None`) keeps today's `width // zbuf_scale` behaviour, which
  is why Auto is retained in `VIDEO_MODES`: it keeps the existing `zbuf_scale` console cvar
  meaningful and preserves the hook for the future dynamic-resolution backlog item (#3).
  Auto is **not** the default (320x240 is); it is simply selectable.

This needs no change to `zbuf_scale`'s default (`ZBUF_SCALE = 4`) and does not touch the
existing renderer/console tests: those assert the bare `Renderer` default and the
`zbuf_scale` cvar's value/clamp/persistence, none of which change.

**Alternative considered:** drop Auto and make `zbuf_scale` a divisor-on-top of the chosen
resolution. Rejected because a sensible default (320x240 at full quality) would require
changing `ZBUF_SCALE` to 1, breaking `test_renderer_zbuf_scale_is_live`, and `80x60` (320/4)
under the current default would be unusable. If you'd rather drop Auto entirely and accept
`zbuf_scale` becoming inert for textured sizing, that's a one-line change to `VIDEO_MODES`.

## Aspect ratio

240x160 is 3:2; stretched into the 4:3 (default 800x600) window it is slightly
vertically stretched — the classic non-square-pixel retro look. 320x240 and 640x480 are 4:3
and match. Letterboxing is intentionally **out of scope** (YAGNI).

## Data flow (one frame, menu open)

```
WndProc: Esc/arrows/Enter -> Menu.key_*   (game input swallowed; mouse ungrabbed)
  Resolution change -> on_select -> Client.set_video_res((w,h)) -> Renderer.video_res
                                                                 -> Renderer.resize -> _setup_zbuf -> zw,zh = (w,h)
Client.frame(dt, do-nothing input) -> renders textured world at zw x zh as usual
                                    -> RenderFrame.menu = Menu.view()
run(): blitter.present(fb=zw x zh, ...) stretches to window; then blitter.draw_menu(view)
```

## Testing

- **`test_menu.py`** (new, no boot): drive the pure `Menu` — arrow navigation wraps/clamps
  as designed, cycling the Resolution choice fires `on_select` with the expected `(w, h)`,
  the Quit action fires its callback, `view()` reports the selected row. Follows the
  standalone `if __name__ == "__main__"` + `print("OK")` convention.
- **Renderer resolution test** (extend `test_console_client.py` or a new test, using a
  `_boot()`-style stack): set `rend.video_res = (320, 240)`, `resize(800, 600)`, assert
  `rend.zw == 320 and rend.zh == 240` and that `render_zbuffer` returns a framebuffer of
  those dimensions; assert `video_res = None` still yields `width // zbuf_scale`.
- **Client default**: assert a freshly constructed `Client` has `video_res == (320, 240)`
  and that it is applied to `rend` after `_load_map` (and persists across a `map` command,
  like `zbuf_scale` does).

## Out of scope

- tkinter frontend (`main.py`).
- Mouse navigation in the menu.
- Window resizing to the chosen resolution.
- Letterboxing / pillarboxing for non-4:3 modes.
- Any menu section other than Video Options (the model is general, but only this section is
  built now).
