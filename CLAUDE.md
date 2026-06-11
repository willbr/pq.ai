# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A working slice of Quake in **pure Python standard library** — `tkinter` is the only
non-stdlib dependency. No numpy, pygame, OpenGL, or C extensions. It loads genuine Quake
shareware data, parses a real BSP, runs id's **actual compiled `progs.dat`** in a QuakeC
VM, and renders three ways (wireframe / flat-shaded / textured software rasteriser).

`README.md` is the authoritative architecture doc — read it first. The per-module
docstrings (top of each `.py`) are detailed and current; trust them over this file for
specifics. Note one easy-to-misread detail: `main.py` runs the QC server **every frame**
with the real frametime (clamped to 100ms), not on a fixed clock; only `nextthink`-gated
thinks (monster AI, etc.) fire at their own ~10 Hz cadence.

## Commands

```bash
python setup.py                 # fetch shareware data + GPL reference source (one-shot, idempotent)
python main.py e1m1             # run the game (gdi32 on Windows, tkinter elsewhere)
python main.py --tk e1m1        # force tkinter on Windows  (e1m1..e1m8, or "start")
python test_pushmove.py         # run one test (prints "OK", or asserts)
export PQ_AUDIO=0; for t in test_*.py; do python "$t"; done   # run all tests
# PQ_AUDIO=0 skips the OS audio backend: headless/sandboxed runs otherwise
# segfault nondeterministically in the CoreAudio callback thread (often after
# printing OK). test_zbuffer_raster.py needs goldens (--regen once).
```

There is **no build step, no linter, no pytest**. Each `test_*.py` is a standalone script
whose `if __name__ == "__main__"` block calls its test functions and prints `OK` on
success (functions are named `test_*`, so a pytest run would also work if installed).

Requires Python 3.13+ and the shareware data at `quake-shareware/id1/pak0.pak` (id
copyright — gitignored; run `python setup.py` to fetch it and the GPL reference source). Sound has a **macOS** backend (`mac.py`,
CoreAudio) and a **Windows** backend (`win.py`, winmm `waveOut`), both via ctypes;
Linux runs muted until a backend is added.

## Architecture (data flow)

The platform-agnostic engine is the **`quake/`** package; the UI-agnostic `Client`
core, both frontends, and the platform audio backends live at the repo root,
**outside** the package, so the engine imports nothing OS- or UI-specific. Inside
`quake/` use **relative imports** (`from .pr_exec import VM`) — including lazy
in-function and `__main__` imports; code outside the package uses absolute
(`from quake.sv import Server`). Run a module self-test with `python -m quake.bsp`
(not `python quake/bsp.py`, which breaks relative imports).

```
quake/pak.py        PAK archive reader → raw lumps by name
  ├─ quake/bsp.py   BSP v29 → flat tuple arrays (faces, planes, leaves, PVS, lightmaps, entity string)
  ├─ quake/mdl.py   .mdl alias models → per-frame float vertex sets
  └─ quake/progs.py progs.dat → bytecode statements, defs, globals buffer (eval_t union)
quake/pr_exec.py    QuakeC VM: opcode loop + flat integer-indexed edict store
quake/sv.py         server: ~70 builtins (pr_cmds.c port), entity spawning, think/movetype loop, player, combat
quake/physics.py    clip-hull tracing + player/monster movement; backs the collision builtins
quake/render.py     three renderers (wireframe, flat, textured z-buffer); lightmaps, light styles, animated surfaces
quake/snd.py        platform-agnostic mixer: decode/spatialize/mix(nframes)→int16 stereo; no OS calls
quake/perf.py       PROFILER singleton: always-on per-frame section timer (server/render/raster/present),
                      EMA-smoothed; section()/begin()/end()/frame_end(); P-key HUD bar chart via bars().
                      The HUD font is Cascadia Mono (Win) / Menlo (mac) -- the stock GDI font and Consolas
                      lack the 1/8-block glyphs the bars draw; win_ui falls back to the stock font if absent
quake/console.py    Quake-style console: command/cvar/alias registry, line editor, history,
                      tab-completion, scrollback; pure (no OS/UI). Client owns one, registers
                      built-ins (render toggles, map, zbuf_scale cvar, god/give, set/echo/clear/
                      alias/exec/help/quit). gdi32 frontend (F1) routes keys + draws the panel.
client.py           UI-agnostic Client: engine stack + camera/player/game state; frame(dt, input)→RenderFrame
main.py             tkinter frontend (all platforms; default off-Windows, or --tk on Windows):
                      after() loop, Canvas/PhotoImage drawing, warp-based mouselook;
                      select_frontend(argv, platform) dispatches to gdi or tk at startup
win_gdi.py          gdi32 Windows frontend (default on Windows): PeekMessage loop, Win32 raw-input
                      mouselook + cursor grab, GdiBlitter (StretchDIBits/Polyline/Polygon/TextOut)
win_ui.py           Windows GDI helpers: GdiBlitter (fast blit + vector/text drawing) plus the
                      raw-input ctypes structs/helpers (RAWINPUT, RAWINPUTDEVICE, raw_mouse_delta,
                      etc.) that win_gdi.py uses for its own WndProc; pure helpers unit-tested in
                      test_win_ui.py
mac.py              macOS audio backend (outside pkg): CoreAudio AudioQueue pulling from the mixer
win.py              Windows audio backend (outside pkg): winmm waveOut buffers, feeder thread pulling from the mixer
```

### Things that will bite you if you don't know them

- **The VM runs id's real compiled game code.** Behaviour you see (doors, monsters, items,
  death) comes from `progs.dat`, not from Python game logic. To change game behaviour you
  usually adjust a *builtin* in `sv.py` or fix the *VM/physics*, not reimplement the rule.
- **Flat integer-indexed memory, mirroring Quake.** Globals are one buffer with aliased
  float/int `memoryview` casts (the `eval_t` union — `progs.gf[o]` / `progs.gi[o]`). All
  edicts share one buffer; edict N's fields start at `N * edict_size` slots. Entity refs
  are stored as the int edict number. Field access goes through `vm.fget_v` /
  `vm.fset_v` with offsets from `pr.field_by_name`.
- **Tests boot the full stack against real shareware data** (Pak → Bsp → Progs → Server →
  Physics → `load_level`), then drive frames. They will fail without `pak0.pak`. Use the
  `_boot()` helpers in existing tests as the pattern for new ones.
- **The bottleneck is tkinter, not Python math** (in wireframe, tkinter frontend).
  Optimisations target Tk: pre-grown line/poly pools, parking unused items off-screen
  via `coords()`, dropping sub-pixel segments. The textured rasteriser renders at 1/4
  resolution (`ZBUF_SCALE`). The gdi32 frontend (`win_gdi.py`) bypasses Tk entirely,
  so its bottleneck is the Python per-pixel fill in the rasteriser.

## Reference source

`quake-source/` is id Software's GPL release kept locally as a read-only port reference
(gitignored; `python setup.py` fetches it). Ports cite their origin (e.g. `SV_RecursiveHullCheck`,
`S_PaintChannels`). When porting more behaviour, match these sources rather than
inventing; it is the standing convention in commit messages and docstrings.

- `WinQuake/` — C engine (the runtime: server, physics, sound, client, renderer).
- `qw-qc/` — QuakeWorld QuakeC (`defs.qc`, `client.qc`, `combat.qc`, `items.qc`,
  `triggers.qc`, …): the *rules* the VM runs. The `.qc` citations in `sv.py` point here.
- `quake-tools/` — id's GPL tool sources (`id-Software/Quake-Tools`), the authoritative
  spec for the data formats the `quake/` parsers read: `qcc/` (QuakeC compiler →
  `progs.dat` bytecode, mirrors `progs.py`/`pr_exec.py`), `qutils/QBSP` (BSP v29 →
  `bsp.py`), `qutils/LIGHT` (lightmaps), `qutils/VIS` (PVS), `qutils/MODELGEN` (`.mdl` →
  `mdl.py`). `QuakeEd/` is the NeXTSTEP map editor, included for completeness.

The two repos: `WinQuake/` + `qw-qc/` come from `id-Software/Quake`; `quake-tools/` from
`id-Software/Quake-Tools`.
