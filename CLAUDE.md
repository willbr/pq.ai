# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A working slice of Quake in **pure Python standard library** — `tkinter` is the only
non-stdlib dependency. No numpy, pygame, OpenGL, or C extensions. It loads genuine Quake
shareware data, parses a real BSP, runs id's **actual compiled `progs.dat`** in a QuakeC
VM, and renders three ways (wireframe / flat-shaded / textured software rasteriser).

`README.md` is the authoritative architecture doc — read it first. The per-module
docstrings (top of each `.py`) are detailed and current; trust them over this file for
specifics. A couple of older docstrings lag the code: the `sv.py` header says physics
builtins are "stubbed" (they are now wired to `physics.py`), and comments mentioning a
"10 Hz" server tick are stale — `main.py` runs the server **every frame** with the real
frametime (clamped to 100ms); only `nextthink`-gated thinks fire at ~10 Hz.

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
copyright — gitignored, download separately). Sound is **macOS-only** (CoreAudio via
ctypes); it degrades gracefully elsewhere.

## Architecture (data flow)

```
pak.py        PAK archive reader → raw lumps by name
  ├─ bsp.py   BSP v29 → flat tuple arrays (faces, planes, leaves, PVS, lightmaps, entity string)
  ├─ mdl.py   .mdl alias models → per-frame float vertex sets
  └─ progs.py progs.dat → bytecode statements, defs, globals buffer (eval_t union)
pr_exec.py    QuakeC VM: opcode loop + flat integer-indexed edict store
sv.py         server: ~70 builtins (pr_cmds.c port), entity spawning, think/movetype loop, player, combat
physics.py    clip-hull tracing + player/monster movement; backs the collision builtins
render.py     three renderers (wireframe, flat, textured z-buffer); lightmaps, light styles, animated surfaces
main.py       tkinter app: input → player edict, game loop, framebuffer scaling
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
