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
