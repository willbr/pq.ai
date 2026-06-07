# pq.ai — Quake in pure Python

A wireframe Quake level walker written in **pure Python standard library**, with
**tkinter as the only UI dependency**. No numpy, no pygame, no OpenGL, no C extensions.

It loads the genuine Quake shareware data, parses a real BSP level, and lets you fly
through it as wireframe 3D — drawn with tkinter `Canvas` lines.

![e1m1 start area](docs/e1m1.png)

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
| `bsp.py` | BSP v29 parser → flat arrays of tuples; entity/spawn parsing |
| `render.py` | Per-frame pipeline: find camera leaf → decompress PVS → backface cull → near-plane clip → perspective project |
| `physics.py` | Clip-hull tracing + player movement (gravity, friction, accel, 18u stairs) — ported from `SV_RecursiveHullCheck` / `SV_WalkMove` |
| `main.py` | tkinter app: mouse-look, movement, game loop, reused Canvas line pool |

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
noclip flight. No entities/monsters yet — that's the next milestone.
