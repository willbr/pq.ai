# Quake-style console

## Goal

Add a Quake-style drop-down console — a command/cvar table with a text input
line, scrollback, history and tab-completion — so engine state can be inspected
and changed at runtime instead of edit-and-rerun. This is `ideas.md` item #2,
and a force multiplier for the perf work that follows: it makes `zbuf_scale`
(the dynamic-resolution lever, item #3) and the render toggles live, and lets
the profiler HUD and map changes be driven without touching code.

Scope decisions (agreed during brainstorming):

- **gdi32 frontend only** (the Windows default). The console *core* is
  UI-agnostic so the tkinter frontend can adopt it later, but only `win_gdi.py`
  wires input and drawing now.
- **A faithful console** — registry, line editor, command history,
  tab-completion, scrollback with scrolling, alias expansion, `exec <file>`,
  and stdout capture. Plus the cheats (`god`, `give`).
- **Toggle key: F1.** Esc also closes the console while it is open.
- **Deferred:** `bind` (no keybinding layer exists — keys are hardcoded in
  `build_input`; the registry is shaped to allow it later) and persistent
  `config.cfg` write / autoexec.

The console core must obey the repo's hard rule: the `quake/` package imports
nothing OS- or UI-specific. `quake/console.py` is pure stdlib, like `perf.py`.

## Architecture

Three layers, matching the engine/UI split:

1. **`quake/console.py`** — pure, UI-agnostic `Console`: registry, scrollback,
   line editor, history, tab-completion, `execute()`. Fully unit-testable with
   no window. The home of all console *mechanism*.
2. **`client.py`** — `Client` owns one `Console`, registers the built-in
   commands and cvars that bind to its own state, and packs the console's
   visible state into the `RenderFrame` when it is open. The home of the
   console *bindings*.
3. **`win_gdi.py` / `win_ui.py`** — route keystrokes into the console and draw
   the drop-down panel. The home of console *I/O*.

## Module: `quake/console.py`

Pure stdlib, no OS/UI imports (relative-import-clean inside the package,
absolute-import-clean from the root frontends — same discipline as `perf.py`).
Single-thread by design: only the frame/input thread touches it.

### `Cvar`

A small record: `name`, `value` (stored as a string, like real Quake), an
optional `default`, an optional `on_change(cvar)` callback fired after a
successful set, and a one-line `help` string. Helpers:

- `as_float()` / `as_int()` — numeric views, tolerant of junk (return the
  default-or-0 on a parse failure, matching Quake's `atof`).
- `as_bool()` — non-zero float is true.

### `Console`

State:

- `commands: dict[str, Command]` — `Command(fn, help)`, `fn(args: list[str])`.
- `cvars: dict[str, Cvar]`.
- `aliases: dict[str, str]` — name → a console line to expand.
- `lines: deque[str]` — wrapped scrollback, capped (e.g. 1024 lines).
- `input: str`, `cursor: int` — the edit line and caret column.
- `history: list[str]`, `hist_pos: int` — entered lines; Up/Down recall.
- `scroll: int` — how many lines up from the bottom the view is pinned
  (PgUp/PgDn), clamped to `[0, len(lines) - 1]`.
- `active: bool` — open/closed.
- `width: int` — column count used to wrap `print()` output (set by the
  frontend from the panel width; defaults to a sane 80).

Registration:

- `register_command(name, fn, help="")`.
- `register_cvar(name, default, on_change=None, help="")` — returns the `Cvar`
  so the caller can read it back later. Re-registering is allowed (idempotent
  at construction).
- `register_alias(name, text)`.

Execution:

- `execute(line)` — the heart. Tokenize with quote handling (double-quoted
  runs are one token; whitespace separates otherwise). Empty line is a no-op.
  Resolution order for token 0:
  1. **alias** → expand to its text and `execute()` that (guard against
     infinite alias recursion with a small depth cap).
  2. **command** → call `fn(args)`; catch exceptions and print them as
     `error: <msg>` rather than crashing the frame.
  3. **cvar** → no args: print ``"<name>" is "<value>"``; with args: set
     `value = args[0]`, fire `on_change`.
  4. else → print `Unknown command "<token0>"`.
- `print(text)` — split on newlines, word-wrap each line to `width`, append to
  `lines`, drop from the front past the cap, and reset `scroll` to 0 (jump to
  the newest output, like Quake). Used both by commands and by stdout capture.

Line editor (called by the frontend per key):

- `key_char(ch)` — insert a printable char at the caret.
- `key_backspace()` / `key_delete()`.
- `key_left()` / `key_right()` / `key_home()` / `key_end()`.
- `key_enter()` — echo `] <input>` into scrollback, push to history, `execute`
  it, clear the line. Blank line just prints `]`.
- `key_up()` / `key_down()` — walk `history` into the edit line.
- `key_tab()` — complete against command + cvar + alias names: a single match
  fills the line; multiple matches print the candidate list and complete to the
  longest common prefix; no match does nothing.
- `key_pageup()` / `key_pagedown()` — move `scroll` by a page (clamped).
- `view_lines(n)` — return the `n` scrollback lines visible given `scroll`, for
  the frontend to draw.

Nothing here knows about keycodes, ctypes, or GDI — the frontend maps native
events to these methods.

## Built-in commands & cvars (registered in `Client`)

`Client.__init__` builds `self.con = Console()` and registers:

**Toggle commands** — `noclip`, `flat`, `zbuf`, `texture`, `prof`. Each calls
the *same* mutation the existing `inp.commands` frozenset drives. To avoid two
copies of that logic, the per-command bodies in `frame()` move into small
`Client` methods (`_toggle_noclip()`, …) that both the frozenset loop and the
console commands call. (The frozenset path stays — the existing N/F/Z/T/P keys
keep working unchanged.)

**`map <name>`** — `_load_map(name)`; print success or the "not in this pak"
message. Reuses the existing missing-map guard.

**`zbuf_scale` cvar** — the dynamic-resolution lever. Refactor: `ZBUF_SCALE`
(module constant in `render.py`) becomes `Renderer.zbuf_scale` (instance
attribute, defaulted from the constant, read in `Renderer.resize` to size
`zw`/`zh`). The cvar's `on_change` clamps to `1..16`, sets
`self.rend.zbuf_scale`, and re-runs `self.rend.resize(*self._view_wh)` to
re-allocate the framebuffer at the new scale. Starts at the current default (4).

**`set <name> <value>`** — bridge to the QuakeC server's cvar dict
(`sv.cvars`), so QC-visible cvars (`skill`, etc.) are reachable. `set` with one
arg prints the current `sv.cvars` value.

**Cheats** — `god` (toggle `FL_GODMODE` on the player edict) and
`give <what> [n]` (health / the four ammo pools / a weapon). Each is a 2–3 line
`sv.py` helper poking the player edict's fields through the existing
`vm.fset_v` / `fget_v`; the console command calls the helper. Guarded to no-op
gracefully if there is no live player edict.

**Utility** — `echo <text>`, `clear` (empty the scrollback), `cmdlist` /
`cvarlist` (list names + help), `help [name]`, `alias <name> <text...>` (or
list aliases with no args), `exec <file>` (read a file of console lines from the
working directory and `execute` each; print and skip on read error), and
`quit` / `exit` (signal shutdown — sets a `Client.quit_requested` flag the
frontend's loop checks).

### `RenderFrame` / `Client.frame()`

`RenderFrame` gains:

```python
console: tuple = None   # (lines, input_line, cursor_col) when open, else None
```

At the end of `frame()`, if `self.con.active`, pack the visible scrollback
(`con.view_lines(rows)` for the panel's row count — the frontend sets
`con.width`/row count on resize), the `] ` input line, and the caret column.
The frontend draws the panel from this; when `None`, no panel.

`frame()` also early-returns a *frozen* render when the console is open? No —
the world keeps simulating and drawing behind the console (Quake behaviour);
the frontend just feeds a neutral `InputState` so the player doesn't move. The
console panel draws on top.

## I/O: `win_gdi.py` + `win_ui.py`

### Key routing (`win_gdi.py`)

`GameWindow` gets `self.console = None`, wired to `client.con` in `run()` after
both objects exist (`_proc` guards on `self.console`).

In `_proc`:

- **F1** (`WM_KEYDOWN`, `VK_F1 = 0x70`) — toggle `console.active`. On *open*:
  clear `self.keys` (no stuck movement), and `ungrab()` if mouselook is engaged
  (cursor becomes visible). Swallow.
- While `console.active`:
  - `WM_CHAR` — `wParam` is the translated character (TranslateMessage already
    runs in `pump()`); printable chars (`0x20..0x7E`) → `console.key_char`.
    Swallow. Backspace/Enter/Tab arrive as both `WM_KEYDOWN` and `WM_CHAR`;
    handle them via `WM_KEYDOWN` and ignore their control-char `WM_CHAR`s.
  - `WM_KEYDOWN` — map `VK_RETURN`→`key_enter`, `VK_BACK`→`key_backspace`,
    `VK_DELETE`→`key_delete`, `VK_TAB`→`key_tab`, `VK_LEFT/RIGHT/HOME/END`,
    `VK_UP/DOWN`→history, `VK_PRIOR/NEXT`→page scroll, `VK_ESCAPE`→close the
    console (do *not* fall through to the quit path). Everything is swallowed —
    no game key (`self.keys`), no command toggle, no mouselook change.

`build_input(dt)` — when `console.active`, return a neutral `InputState`
(zero movement/turn/look, `fire=False`, `impulse=0`, empty `commands`;
`mouselook` reflects the real grab state for the HUD prompt only). Otherwise
unchanged.

The `run()` loop also checks `client.quit_requested` (set by the `quit`
command) to break.

### Drawing (`win_ui.py` `GdiBlitter`)

A new `draw_console(lines, input_line, cursor_col, cw, ch)`:

- Fill a drop-down panel — a dark rectangle (`FillRect` with a dark-grey brush)
  across the full width, from the top down to ~40% of the client height, with a
  brighter 1px bottom edge so it reads as a panel.
- Draw the scrollback lines (top-to-bottom) and the `] input_` line at the
  panel bottom, in the **same monospace HUD font** the profiler bars use
  (Cascadia Mono → fallback), so columns align. Draw a caret (a `|` or a filled
  cell) at `cursor_col` on the input line.

`win_gdi.run()` calls `draw_console(...)` after the world present, when
`rf.console` is set, passing the panel-sized line list.

### Stdout capture

In `win_gdi.run()`, after the client exists, install a tee on `sys.stdout`:
a tiny wrapper that forwards `write()` to both the real stdout and
`client.con.print(...)` (buffering partial lines until a newline). This makes
the engine's existing `print()` diagnostics ("changelevel … not in this pak",
mdl/bsp load failures) appear in the console with no per-callsite edits.
Restored on shutdown.

## Testing

`test_console.py` — pure, no window, no shareware data:

- tokenize: quotes group, whitespace splits, empty line is a no-op.
- command dispatch passes args; a throwing command prints `error:` and does not
  propagate.
- cvar: bare name prints, `name value` sets and fires `on_change`; numeric
  views tolerate junk.
- unknown command prints the `Unknown command` line.
- line editor: char/backspace/delete/left/right/home/end on the caret.
- history: Up/Down recall in order.
- tab-completion: unique prefix completes; ambiguous lists candidates and fills
  the common prefix; no match is a no-op.
- scrollback: cap drops oldest; `print` resets scroll; PgUp/PgDn clamp.
- alias expands (and recursion is bounded).
- `print` word-wraps to `width`.

`test_console_client.py` (or a case in an existing boot test) — boots the real
stack (`Pak→Bsp→Progs→Server`, like the other tests; skips/asserts on
`pak0.pak`) and checks the bindings: `con.execute("noclip")` flips
`client.noclip`; `con.execute("zbuf_scale 8")` sets `rend.zbuf_scale` and the
framebuffer resizes; `con.execute("map e1m2")` changelevels (or prints the
guard for a missing map). Follows the `_boot()` pattern in existing tests.

No new dependency; no build step. Each test file prints `OK` from its
`__main__` block.

## Files touched

- **new** `quake/console.py` — the pure console core.
- **new** `test_console.py`, `test_console_client.py`.
- `render.py` — `ZBUF_SCALE` constant → `Renderer.zbuf_scale` instance attr.
- `client.py` — own a `Console`, register built-ins, extract `_toggle_*`
  helpers, pack `RenderFrame.console`, add `quit_requested`; small `sv.py`
  cheat helpers (`god`, `give`).
- `win_gdi.py` — F1/Esc + console key routing, neutral input while open,
  stdout tee, quit flag, `draw_console` call.
- `win_ui.py` — `GdiBlitter.draw_console`.
- `ideas.md` — tick item #2.
