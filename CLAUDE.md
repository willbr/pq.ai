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
python3 main.py e1m1            # run the game (e1m1..e1m8, or "start")
python3 test_pushmove.py        # run one test (prints "OK", or asserts)
for t in test_*.py; do python3 "$t"; done   # run all tests
```

There is **no build step, no linter, no pytest**. Each `test_*.py` is a standalone script
whose `if __name__ == "__main__"` block calls its test functions and prints `OK` on
success (functions are named `test_*`, so a pytest run would also work if installed).

Requires Python 3.13+ and the shareware data at `quake-shareware/id1/pak0.pak` (id
copyright — gitignored, download separately). Sound has a **macOS** backend (`mac.py`,
CoreAudio) and a **Windows** backend (`win.py`, winmm `waveOut`), both via ctypes;
Linux runs muted until a backend is added.

## Architecture (data flow)

The platform-agnostic engine is the **`quake/`** package; the tkinter UI (`main.py`) and
the platform audio backend (`mac.py`) live at the repo root, **outside** the package, so
the engine imports nothing OS- or UI-specific. Inside `quake/` use **relative imports**
(`from .pr_exec import VM`) — including lazy in-function and `__main__` imports; code
outside the package uses absolute (`from quake.sv import Server`). Run a module self-test
with `python3 -m quake.bsp` (not `python3 quake/bsp.py`, which breaks relative imports).

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
mac.py              macOS audio backend (outside pkg): CoreAudio AudioQueue pulling from the mixer
win.py              Windows audio backend (outside pkg): winmm waveOut buffers, feeder thread pulling from the mixer
main.py             tkinter app (outside pkg): input → player edict, game loop, framebuffer; picks audio backend by sys.platform
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
- **The bottleneck is tkinter, not Python math** (in wireframe). Optimisations target Tk:
  pre-grown line/poly pools, parking unused items off-screen via `coords()`, dropping
  sub-pixel segments. The textured rasteriser renders at 1/4 resolution (`ZBUF_SCALE`).

## Reference source

`quake-source/` is id Software's GPL release kept locally as a read-only port reference —
`WinQuake/` (C engine) and `qw-qc/` (QuakeC). Ports cite their origin (e.g.
`SV_RecursiveHullCheck`, `S_PaintChannels`). When porting more behaviour, match these
sources rather than inventing; it is the standing convention in commit messages and
docstrings.
