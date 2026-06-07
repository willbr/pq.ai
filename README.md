# pq.ai — Quake in pure Python

A wireframe Quake level walker written in **pure Python standard library**, with
**tkinter as the only UI dependency**. No numpy, no pygame, no OpenGL, no C extensions.

It loads the genuine Quake shareware data, parses a real BSP level, and lets you fly
through it as wireframe 3D — drawn with tkinter `Canvas` lines.

![e1m1 wireframe](docs/e1m1.png)
![e1m1 flat-shaded](docs/e1m1_flat.png)

## Run

You need the Quake shareware data (id Software copyright — free to download, not
redistributed here):

```
quake-shareware/id1/pak0.pak
```

Then:

```bash
python3 main.py e1m1        # also: e1m2 … e1m8, start
```

**Controls:** click the window to capture the mouse, then `WASD` + mouse to fly.
`Space`/`Ctrl` up/down, `Shift` faster, `Tab` toggle mouse-look, `Esc` release/quit.

## How it works

| File | Role |
|------|------|
| `pak.py` | PAK archive reader (`"PACK"` header + 64-byte directory entries) |
| `bsp.py` | BSP v29 parser → flat arrays of tuples; entity/spawn parsing; texinfo + embedded miptex decode |
| `mdl.py` | Alias model (`.mdl`) reader: header, skins, triangles, and per-frame vertex sets (single + time-animated groups), decoded to float positions |
| `progs.py` | `progs.dat` (QuakeC v6) loader: statements, defs, functions, a growable string heap, and the globals block as one buffer with aliased float/int views (the `eval_t` union) |
| `pr_exec.py` | The QuakeC bytecode interpreter — `PR_ExecuteProgram`'s opcode loop, call frames, and a flat integer-indexed edict store (all edict fields in one buffer, edict *N* at *N·edict_size*) |
| `sv.py` | Server layer: the ~65 builtins (`pr_cmds.c`), entity spawning from the BSP string (`ED_LoadFromFile`), and the think/movetype frame loop. Runs id's **actual compiled game code** |
| `render.py` | Two renderers — **wireframe** (PVS → backface cull → near-clip edges → project) and **flat-shaded** (BSP back-to-front painter's order → near-clip polygons → filled `create_polygon`). Faces are tinted by each texture's average colour (sampled from the embedded miptex + the Quake palette) and lit by a static directional light. Draws the world, brush-model **entities** (at the origins the QC sets), and **alias models** (monsters/items — rotated, animated, woven into the painter's walk by bounding box), all PVS-culled |
| `physics.py` | Clip-hull tracing + player movement (gravity, friction, accel, 18u stairs) — ported from `SV_RecursiveHullCheck` / `SV_WalkMove` |
| `main.py` | tkinter app: mouse-look, movement, game loop, reused Canvas line pool; ticks the QC server at a fixed 10 Hz |

The trick that makes it fast enough: wireframe needs **no framebuffer**. We draw edges
with `Canvas.create_line` (C-implemented), and PVS + backface culling cut a ~5,500-face
level down to a few hundred visible edges per frame.

**Where the time goes:** the Python render math is only ~2 ms/frame — the bottleneck is
tkinter rasterizing the lines (`update()`). So the optimizations that matter all reduce
work *for Tk*: a pre-grown line pool (no `create_line` hitches), parking unused lines
off-screen with `coords()` instead of `itemconfig(state=...)` (no redraw churn), and
dropping sub-pixel segments (far edges too small to see). Typical: ~520 fps on e1m1,
~60 on e1m2, ~30 on the open `start` hub. (Frustum culling and depth-shading were
tried and removed — measurement showed they added cost without reducing the line count
Tk actually draws.)

## Status

Loads, renders, and **walks** through all episode-1 shareware maps with real Quake
collision: gravity, floor/wall sliding, stair stepping, and jumping. Press `N` for
noclip flight, `F` to toggle **flat shading** (Tk `create_polygon`, drawn
back-to-front via the BSP — no z-buffer needed).

A **QuakeC virtual machine** (`progs.py` + `pr_exec.py` + `sv.py`) runs the genuine
`progs.dat` game logic: it spawns the whole entity list, runs each spawn function,
and ticks every entity's think chain at 10 Hz. Doors, lifts and buttons are real
entities — their brush models are drawn at the origins the QC sets — and invisible
trigger volumes correctly stop rendering (the static renderer used to draw them as
solid blocks). Monsters and items are drawn as **animated `.mdl` models**, their
frames advancing through the real QuakeC animation chains (a grunt cycles its eight
`army_stand` frames via the `STATE` opcode, etc.).

**Still stubbed:** collision builtins (`traceline`, `walkmove`, `droptofloor`, …)
return clear-path defaults, and there's no player entity in the simulation yet — so
monsters stand and animate but don't navigate, and nothing *triggers* the doors as
you walk through. Wiring the builtins to `physics.py` (and adding a player edict so
touch/trigger works) is the next milestone.
