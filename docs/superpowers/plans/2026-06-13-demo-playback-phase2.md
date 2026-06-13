# Phase 2: Demo Playback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Play genuine Quake `.dem` files — the shareware `demo1/2/3.dem` in `pak0.pak` and any external demo — by feeding the Phase-1 client parser (`cl`) from a demo file instead of a live server, with the renderer driven entirely from `cl`. Add `playdemo`/`stop`, `timedemo`, and the title-screen demo loop.

**Architecture:** A `.dem` is a recording of the server→client message stream Phase 1 already parses. Phase 2 adds `quake/demo.py` (file framing), stands up the render stack from the demo's *signon* (no server), and adds a **distinct demo frame path** in `client.py` that advances `cl.time`, reads/parses demo messages on Quake's timing gate, relinks, and drives the camera/HUD/view from `cl`. Live play is left untouched (no regression risk); a small set of `_cur_*` accessors route the shared render block to `sv` (live) or `cl` (demo).

**Tech Stack:** Pure Python 3.13 stdlib. Builds entirely on Phase 1's `quake/{msg,protocol,cl_parse}.py`. Tests are standalone scripts (`import _bootstrap`, print `OK`), run muted with `PQ_AUDIO=0`.

**Reference:** `quake-source/WinQuake/cl_demo.c` (framing, `CL_PlayDemo_f`, `CL_GetMessage` timing gate, `CL_NextDemo`/`startdemos`, `CL_FinishTimeDemo`), `cl_parse.c`, `cl_main.c`. Phase-1 spec/plan in `docs/superpowers/`.

**Demo file format (confirmed against `pak0.pak`):** an ASCII CD-track number terminated by `\n` (e.g. `2\n`), then repeated frames `[u32 little-endian msg length][3×f32 little-endian viewangles][length bytes of svc_* message]`. The first message is the large signon (serverinfo + baselines + lightstyles + setview + signonnum).

---

## How playback differs from the live loopback (the core of this phase)

Live `frame()` (after Phase 1): apply input → `sv.run_frame` → `build_datagram` → `cl.parse_message` → `cl.relink` → render world entities from `cl`, but camera/HUD/view/dlights/time from `sv`.

Demo `frame()`: **no server, no input, no physics.** Instead: advance `cl.time += dt`; while `cl.time >= cl.mtime[0]` read the next demo message, set `cl.viewangles` from its 3-float header, and `cl.parse_message` it; then `cl.relink`; then render with the camera/HUD/view/dlights/time **from `cl`**. The signon (first message) is parsed once at `playdemo` time and used to build the render stack.

The timing gate (`cl_demo.c` `CL_GetMessage`): in normal playback a new message is read only when `cl.time >= cl.mtime[0]` (so messages play at their recorded cadence and `relink` interpolates between them at render rate). In `timedemo` one message is read per frame regardless of time (run as fast as possible).

---

## File structure

| File | Responsibility | Status |
|------|----------------|--------|
| `quake/demo.py` | `DemoReader` (parse CD-track header + per-frame `[len][angles][msg]`); pure | create |
| `quake/cl_parse.py` | extend: client-visible surface for demo (`hud_status`, `light_entities`, `view_weapon`, `intermission_*`, `player_health`, `dlight_events`, `time`/`lightstyles`/`particles` already present); temp-entity → dlight events | modify |
| `client.py` | demo controller state, `_load_demo` (server-free render stack), demo branch in `frame()`, `_cur_*` accessors, `playdemo`/`stop`/`timedemo` commands, `_load_render_models` extracted from `_load_map` | modify |
| `main.py` | default to `start`; `start`/no-map launches the title demo loop | modify |
| `tests/test_demo.py` | framing round-trip + real `demo1.dem` header/first-frame parse | create |
| `tests/test_demo_playback.py` | boot + play `demo1.dem` N frames headless; spot-check camera/entities | create |

---

## Task 1: Demo file framing (`quake/demo.py`)

**Files:**
- Create: `quake/demo.py`
- Test: `tests/test_demo.py`

`DemoReader` wraps a `bytes` blob: parse the CD-track header line, then iterate frames. Each `next_frame()` returns `(viewangles_tuple, message_bytes)` or `None` at EOF.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_demo.py
"""Demo file framing tests (quake/demo.py) against WinQuake cl_demo.c and the
real shareware demo1.dem. Run muted: PQ_AUDIO=0 python tests/test_demo.py."""
import _bootstrap  # noqa: F401
import struct
from quake.demo import DemoReader, write_demo_frame
from quake.pak import Pak

PAK = "quake-shareware/id1/pak0.pak"


def test_synthetic_frame_roundtrip():
    body = bytes(range(20))
    blob = b"0\n" + struct.pack("<i", len(body)) + struct.pack("<3f", 1.0, 2.0, 3.0) + body
    r = DemoReader(blob)
    assert r.cdtrack == "0"
    ang, msg = r.next_frame()
    assert msg == body
    assert abs(ang[0] - 1.0) < 1e-6 and abs(ang[2] - 3.0) < 1e-6
    assert r.next_frame() is None          # EOF


def test_real_demo1_header_and_first_frame():
    blob = Pak(PAK).read("demo1.dem")
    r = DemoReader(blob)
    assert r.cdtrack == "2"                 # demo1.dem CD track
    ang, msg = r.next_frame()
    assert len(msg) > 1000                  # the big signon message
    assert msg[0] == 11                     # svc_serverinfo is the first byte


def test_write_demo_frame_matches_reader():
    out = bytearray(b"3\n")
    out += write_demo_frame((0.0, 90.0, 0.0), b"\x01\x02\x03")
    r = DemoReader(bytes(out))
    assert r.cdtrack == "3"
    ang, msg = r.next_frame()
    assert msg == b"\x01\x02\x03" and abs(ang[1] - 90.0) < 1e-6


if __name__ == "__main__":
    test_synthetic_frame_roundtrip()
    test_real_demo1_header_and_first_frame()
    test_write_demo_frame_matches_reader()
    print("OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_demo.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'quake.demo'`.

- [ ] **Step 3: Implement `quake/demo.py`**

```python
# quake/demo.py
"""Quake .dem demo file framing -- the read/write halves of WinQuake cl_demo.c.
A demo is an ASCII CD-track number terminated by '\\n', then repeated frames:
  [int32 LE message length][3 x float32 LE viewangles][length bytes of svc_* msg]
The message bytes are exactly what quake.cl_parse.ClientState.parse_message
consumes; DemoReader yields them one frame at a time. write_demo_frame is the
recording counterpart (used in Phase 3). Pure stdlib."""
import struct


class DemoReader:
    """Reads a .dem blob frame by frame. `cdtrack` is the header track string;
    next_frame() returns (viewangles, message_bytes) or None at end."""

    def __init__(self, data):
        self.data = data
        nl = data.find(b"\n")
        if nl < 0:
            raise ValueError("demo: no CD-track header line")
        self.cdtrack = data[:nl].decode("latin-1").strip()
        self.pos = nl + 1

    def next_frame(self):
        d = self.data
        if self.pos + 16 > len(d):
            return None                        # no room for len+angles header
        (length,) = struct.unpack_from("<i", d, self.pos)
        angles = struct.unpack_from("<3f", d, self.pos + 4)
        start = self.pos + 16
        end = start + length
        if end > len(d):
            return None                        # truncated final frame
        self.pos = end
        return angles, d[start:end]


def write_demo_frame(viewangles, message):
    """Frame a single message for a .dem file (CL_WriteDemoMessage): the length,
    the 3 viewangles, then the message bytes. Returns the bytes to append."""
    out = struct.pack("<i", len(message))
    out += struct.pack("<3f", viewangles[0], viewangles[1], viewangles[2])
    return out + bytes(message)


if __name__ == "__main__":                     # python -m quake.demo
    blob = b"1\n" + write_demo_frame((0.0, 0.0, 0.0), b"\x07")
    r = DemoReader(blob)
    assert r.cdtrack == "1" and r.next_frame()[1] == b"\x07"
    print("quake.demo OK")
```

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_demo.py` → `OK`; `python -m quake.demo` → `quake.demo OK`.

- [ ] **Step 5: Commit**

```bash
git add quake/demo.py tests/test_demo.py
git commit -m "demo: .dem file framing (DemoReader + write_demo_frame)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Client-visible surface on `cl` for demo rendering

**Files:**
- Modify: `quake/cl_parse.py`
- Test: `tests/test_cl_parse.py` (add cases)

The render block reads these off the live `sv`: `hud_status()` (dict), `light_entities()`, `view_weapon()`, `intermission_active()`/`intermission_stats()`, `player_health()`, `lightstyles`, `particles`, `time`, and `dlight_events`. Demo mode reads the equivalents off `cl`. `SceneFromClient` already exposes `particles`/`lightstyles`/`time`. Add the rest so a single `source` object covers the whole surface.

First READ `quake/sv.py`'s `hud_status()` to copy its exact dict keys and the weapon-name / keys / powerups decoding (item bit constants), so the cl version produces an identical dict shape.

- [ ] **Step 1: Add failing tests**

Add to `tests/test_cl_parse.py` (+ `__main__`):

```python
def test_scene_hud_status_from_stats():
    from quake.cl_parse import ClientState, SceneFromClient
    from quake import protocol as P
    cl = ClientState()
    cl.stats[P.STAT_HEALTH] = 87
    cl.stats[P.STAT_ARMOR] = 50
    cl.stats[P.STAT_SHELLS] = 25
    cl.stats[P.STAT_ACTIVEWEAPON] = 0
    cl.items = 1                                 # IT_AXE
    s = SceneFromClient(cl).hud_status()
    assert s["health"] == 87 and s["armor"] == 50 and s["shells"] == 25
    assert "weapon" in s and "items" in s


def test_scene_light_entities_from_effects():
    from quake.cl_parse import ClientState, SceneFromClient, ClEntity
    cl = ClientState()
    e = cl.entity(3)
    e.model = "progs/missile.mdl"
    e.origin = (10.0, 20.0, 30.0)
    e.effects = 0
    e.msgtime = cl.mtime[0]
    out = SceneFromClient(cl).light_entities()
    # a rocket model emits a glow even with no EF_ bits (is_rocket True)
    assert any(num == 3 for num, org, eff, rocket in out)
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_cl_parse.py`
Expected: FAIL — `SceneFromClient` has no `hud_status`.

- [ ] **Step 3: Extend `SceneFromClient` (and add a temp-entity → dlight hook)**

In `quake/cl_parse.py`, add to `SceneFromClient` (mirror `sv.hud_status()`'s dict
exactly — copy the weapon-name table and item-bit decode from `sv.py`):

```python
    # --- full client-visible surface for demo-mode rendering ---
    def time(self):
        return self.cl.time
    # (note: `time` is already a property below if present; keep ONE definition.
    #  If a `time` property already exists, leave it and skip this.)

    def player_health(self):
        from quake import protocol as P
        return self.cl.stats[P.STAT_HEALTH]

    def hud_status(self):
        """Same dict shape as Server.hud_status(), sourced from cl.stats/items.
        Copy the weapon-name lookup and keys/powerups item-bit decode verbatim
        from quake/sv.py's hud_status so the sbar renders identically."""
        from quake import protocol as P
        st = self.cl.stats
        items = self.cl.items
        # ... build weapon name from st[STAT_ACTIVEWEAPON]/items, keys/powerups
        #     from item bits, exactly as sv.hud_status does ...
        return {
            "health": st[P.STAT_HEALTH], "armor": st[P.STAT_ARMOR],
            "weapon": _weapon_name(items), "ammo": st[P.STAT_AMMO],
            "shells": st[P.STAT_SHELLS], "nails": st[P.STAT_NAILS],
            "rockets": st[P.STAT_ROCKETS], "cells": st[P.STAT_CELLS],
            "keys": _keys_str(items), "powerups": _powerups_str(items),
            "items": items, "weapon_bit": items,
        }

    def light_entities(self):
        """Entities carrying engine light effects, like sv.light_entities():
        (num, origin, effects, is_rocket). is_rocket keys off the model name
        (rocket/grenade .mdl glow). Mirrors CL_RelinkEntities' dlight logic."""
        out = []
        for num, e in enumerate(self.cl.entities):
            if e is None or not e.model or num == self.cl.viewentity:
                continue
            name = e.model
            is_rocket = name.endswith("missile.mdl") or "lavaball" in name
            if e.effects or is_rocket:
                out.append((num, e.origin, e.effects, is_rocket))
        return out

    @property
    def dlight_events(self):
        return self.cl.dlight_events

    def view_weapon(self):
        """(view-model path, frame) from clientdata: STAT_WEAPON is the
        modelindex of the v_*.mdl in the precache; STAT_WEAPONFRAME the frame."""
        from quake import protocol as P
        mi = self.cl.stats[P.STAT_WEAPON]
        if mi <= 0 or mi >= len(self.cl.model_precache):
            return None
        return (self.cl.model_precache[mi], self.cl.stats[P.STAT_WEAPONFRAME])

    def intermission_active(self):
        return self.cl.intermission

    def intermission_stats(self):
        from quake import protocol as P
        if not self.cl.intermission:
            return None
        return {"time": int(self.cl.completed_time),
                "secrets": self.cl.stats[P.STAT_SECRETS],
                "total_secrets": self.cl.stats[P.STAT_TOTALSECRETS],
                "monsters": self.cl.stats[P.STAT_MONSTERS],
                "total_monsters": self.cl.stats[P.STAT_TOTALMONSTERS]}
```

Add the module-level `_weapon_name`/`_keys_str`/`_powerups_str` helpers by
copying the exact item-bit logic out of `sv.hud_status()` (DRY: if practical,
move that decode into a small shared function both call). Add `self.cl.completed_time = 0.0`
to `ClientState.__init__`, set from the intermission message if the demo carries it
(otherwise leave 0 — the demo's centerprint covers the visible text).

In `ClientState.__init__` ensure `self.dlight_events = []` exists. In
`parse_temp_entity`, for explosion-class effects append a dlight event the way
the live server does (so demo explosions flash):

```python
        if kind in (P.TE_EXPLOSION, P.TE_TAREXPLOSION):
            self.dlight_events.append((org, 350.0, self.mtime[0] + 0.5, 300.0))
```
(`org` = the explosion origin read from the message; capture it where the kind is
parsed.) Clear `self.dlight_events` is the consumer's job (client `_update_dlights`
already drains `dlight_events` each frame — confirm and mirror).

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_cl_parse.py` → `OK`. Re-run `tests/test_sv_send.py` → `OK`.

- [ ] **Step 5: Commit**

```bash
git add quake/cl_parse.py tests/test_cl_parse.py
git commit -m "cl_parse: full client-visible surface on SceneFromClient for demo rendering

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Server-free render-stack setup from the demo signon

**Files:**
- Modify: `client.py`
- Test: `tests/test_demo_playback.py` (created here, expanded in Task 4)

`_load_map` builds `self.bsp/rend/phys/models/smodels/bmodels` from `sv.model_precache`. Extract the model-loading loop into `_load_render_models(precache)` so both live and demo share it (DRY). Add `_load_demo(blob)`: parse the signon into a fresh `cl`, take the map name from `cl.model_precache[1]`, build the render stack from `cl.model_precache`, **without** creating a `Server`.

First READ `client.py` `_load_map` (lines ~276-406) to see the exact model/sprite/pickup loading loops and the palette/sbar already on `self`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_demo_playback.py
"""Headless demo playback smoke test. Run: PQ_AUDIO=0 python tests/test_demo_playback.py."""
import _bootstrap  # noqa: F401
from client import Client
from quake.pak import Pak

PAK = "quake-shareware/id1/pak0.pak"


def test_load_demo_builds_render_stack_without_server():
    c = Client.__new__(Client)             # bypass __init__'s _load_map
    Client._init_assets_only(c)            # palette/sbar/console/mixer, no map
    blob = Pak(PAK).read("demo1.dem")
    c._load_demo(blob)
    assert c.bsp is not None and c.rend is not None
    assert c.mapname and c.cl.model_precache[1].endswith(".bsp")
    assert len(c.models) == len(c.cl.model_precache)
    assert c.demo is not None              # demo controller active


if __name__ == "__main__":
    test_load_demo_builds_render_stack_without_server()
    print("OK")
```

(Note: this test introduces `_init_assets_only` and `_load_demo`. If a clean
`__init__`-split is too invasive, an acceptable alternative is `Client("e1m1")`
then `c._load_demo(blob)` — adjust the test to that and drop `_init_assets_only`.
Choose the lower-risk option and keep the test asserting the same end state.)

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py`
Expected: FAIL — `_load_demo` / `_init_assets_only` missing.

- [ ] **Step 3: Refactor `_load_map` and add `_load_demo`**

In `client.py`:
1. Extract the model/sprite/pickup loading loop (the three `for idx, name in enumerate(self.sv.model_precache)` blocks plus `self._vmodels = {}`) into:
   ```python
   def _load_render_models(self, model_precache):
       """Load .mdl/.spr/external-.bsp render models indexed to match
       modelindex, from a precache name list (sv's or a demo's cl)."""
       self.models = [None] * len(model_precache)
       self.smodels = [None] * len(model_precache)
       self.bmodels = [None] * len(model_precache)
       for idx, name in enumerate(model_precache):
           # ... the existing Mdl/Spr/PickupModel loads, keyed on extension ...
       self._vmodels = {}
   ```
   and call it from `_load_map` with `self.sv.model_precache`.
2. Add `_load_demo`:
   ```python
   def _load_demo(self, blob):
       """Set up the render stack to play a .dem: parse the signon, take the
       map + precache from cl, build bsp/renderer/physics/models WITHOUT a
       server. Starts demo playback (self.demo)."""
       from quake.demo import DemoReader
       from quake.cl_parse import ClientState, SceneFromClient
       reader = DemoReader(blob)
       self.cl = ClientState()
       # the first frame is the signon; parse it so cl.model_precache is filled
       first = reader.next_frame()
       if first is None:
           self.con.print("demo: empty file"); return False
       self.cl.viewangles = list(first[0])
       self.cl.parse_message(__import__("quake.msg", fromlist=["MsgReader"]).MsgReader(first[1]))
       mappath = self.cl.model_precache[1]            # "maps/xxx.bsp"
       self.mapname = mappath[len("maps/"):-len(".bsp")]
       self.bsp = Bsp(self.pak.read(mappath))
       self.rend = Renderer(self.bsp, self.palette, self.colormap)
       self.rend.zbuf_scale = self._zbuf_scale
       self.rend.pixel_aspect = self._pixel_aspect
       self.rend.video_res = self.video_res
       self.phys = Physics(self.bsp)
       self._load_render_models(self.cl.model_precache)
       # sound precache for the demo (so svc_sound could play later)
       self.mixer.stop_all()
       for name in self.cl.sound_precache[1:]:
           p = "sound/" + name
           if p in self.pak.files:
               self.mixer.precache(name, self.pak.read(p))
       self.scene = SceneFromClient(self.cl)
       self.sv = None                                  # no server in demo mode
       # CL_ClearState cosmetic HUD timers (mirror _load_map)
       self.item_gettime = [0.0] * 32
       self._prev_items = 0
       self.faceanimtime = 0.0
       self.dlights = {}
       self.intermission = False
       if self._view_wh != (0, 0):
           self.rend.resize(*self._view_wh)
       self.demo = Demo(reader)                         # controller (Task 4)
       return True
   ```
   (`Demo` is defined in Task 4 — if implementing Task 3 first, stub `self.demo = ("demo", reader)` and replace in Task 4, or implement Task 4's `Demo` class now.)
3. Add `_init_assets_only(self)` that runs the server-independent half of
   `__init__` (pak/palette/colormap/sbar/confont/conback/mixer/console/render
   flags/menu/the persisted `_zbuf_scale`/`_pixel_aspect`/`video_res`/`_view_wh`),
   WITHOUT `_load_map`. Refactor `__init__` to call `_init_assets_only(self)` then
   `_load_map(mapname)` so the two share one definition (DRY). Keep `__init__`'s
   external behavior identical for live play.

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py` → `OK`.
Run the full suite to ensure the `__init__`/`_load_map` refactor didn't regress:
`export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done`
Expected: all `ok` (test_win_ui is a Windows skip).

- [ ] **Step 5: Commit**

```bash
git add client.py tests/test_demo_playback.py
git commit -m "client: build the render stack from a demo signon, no server (_load_demo)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Demo frame path + `playdemo`/`stop`

**Files:**
- Modify: `client.py`
- Test: `tests/test_demo_playback.py` (expand)

Add the `Demo` controller, the demo branch in `frame()`, the `_cur_*` accessors routing the render block to `cl` vs `sv`, and the console commands. This is the integration core — make surgical edits and lean on the full suite + smoke test.

READ `client.py` `frame()` (lines ~1084-1451) before editing, especially the camera-eye computation (~1221-1266), the `st = self.sv.hud_status()` read (~1271), `_update_dlights` (~514), and the lightstyles/particles/time reads in the render block (~1297-1355).

- [ ] **Step 1: Add the failing playback test**

Add to `tests/test_demo_playback.py` (+ `__main__`):

```python
def test_play_demo1_advances_and_renders():
    from client import Client, InputState
    from quake.pak import Pak
    c = Client.__new__(Client); Client._init_assets_only(c)
    c._load_demo(Pak(PAK).read("demo1.dem"))
    c.resize(640, 480)
    last_org = None
    moved = False
    for _ in range(120):                       # ~6s at dt=0.05
        rf = c.frame(0.05, InputState())
        assert rf is not None
        org = tuple(c.pos)
        if last_org is not None and org != last_org:
            moved = True
        last_org = org
    assert moved, "demo camera never moved"
    assert len(c.scene.alias_entities()) >= 0  # rendered without exception
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py`
Expected: FAIL — `frame()` has no demo branch / `Demo` undefined.

- [ ] **Step 3: Implement the `Demo` controller, frame branch, and accessors**

In `client.py`:

1. A small controller (module-level class):
```python
class Demo:
    """Drives .dem playback: holds the DemoReader and the timing/timedemo state
    (cl_demo.c CL_GetMessage). Reading is gated on cl.time so messages play at
    their recorded cadence; timedemo reads one per frame and reports fps."""
    def __init__(self, reader, timedemo=False):
        self.reader = reader
        self.timedemo = timedemo
        self.finished = False
        self.frames = 0          # timedemo frame counter
        self.start_time = None   # wall clock at timedemo frame 1
```

2. In `__init__`/`_init_assets_only`, initialise `self.demo = None`.

3. At the TOP of `frame()`, before the live server block, branch:
```python
        if self.demo is not None:
            return self._demo_frame(dt, inp)
```

4. Add `_demo_frame` — the playback path:
```python
    def _demo_frame(self, dt, inp):
        """One frame of .dem playback: advance cl.time, read+parse demo messages
        on the timing gate, relink, drive the camera/HUD from cl, render."""
        import time as _time
        from quake.msg import MsgReader
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)
        self._uptime += dt
        PROFILER.begin("server")
        d = self.demo
        # ESC menu / console pause playback (host.c pauses the demo clock too)
        paused = self.menu.active or self.con.active
        if not paused and not d.finished:
            self.cl.time += dt
            # read messages until caught up (timedemo: exactly one per frame)
            while True:
                if not d.timedemo and self.cl.time < self.cl.mtime[0]:
                    break
                fr = d.reader.next_frame()
                if fr is None:
                    d.finished = True
                    break
                self.cl.viewangles = list(fr[0])
                self.cl.parse_message(MsgReader(fr[1]))
                if d.timedemo:
                    if d.start_time is None:
                        d.start_time = _time.monotonic()  # clock starts frame 1
                    d.frames += 1
                    break
            self.cl.relink(dt)
            if d.timedemo and d.finished:
                self._finish_timedemo()
        PROFILER.end("server")
        # drive the camera from cl
        ve = self.cl.entities[self.cl.viewentity]
        org = ve.origin if ve else (0.0, 0.0, 0.0)
        self.pos = [org[0], org[1], org[2]]
        self.yaw = self.cl.viewangles[1]
        self.pitch = self.cl.viewangles[0]
        self.view_angles = (self.cl.viewangles[0], self.cl.viewangles[1],
                            self.cl.viewangles[2])
        eye = (self.pos[0], self.pos[1], self.pos[2] + self.cl.view_height)
        return self._render_scene(dt, eye, demo=True)
```

5. Factor the render half of the live `frame()` (from `_update_palette`/
   `_update_dlights` through building and returning the `RenderFrame`) into a
   shared `_render_scene(self, dt, eye, demo)` helper that the live path also
   calls. Inside it, replace the direct `self.sv.X` reads in the render block
   with `self._cur_*()` accessors:
```python
    def _cur_time(self):
        return self.cl.time if self.demo else self.sv.time
    def _cur_hud(self):
        return self.scene.hud_status() if self.demo else self.sv.hud_status()
    def _cur_lightstyles(self):
        return self.cl.lightstyles if self.demo else self.sv.lightstyles
    def _cur_particles(self):
        return self.cl.particles if self.demo else self.sv.particles
    def _cur_health(self):
        return self.scene.player_health() if self.demo else self.sv.player_health()
    def _cur_intermission(self):
        return (self.scene.intermission_active() if self.demo
                else self.sv.intermission_active())
    def _cur_view_weapon(self):
        return self.scene.view_weapon() if self.demo else self.sv.view_weapon()
    def _light_source(self):
        return self.scene if self.demo else self.sv     # for _update_dlights
```
   In `_update_dlights`, read `src = self._light_source()` and use
   `src.light_entities()` / `src.dlight_events`. In `_view_model`, use
   `self._cur_view_weapon()`. In the render block, use `_cur_time`/`_cur_hud`/
   `_cur_lightstyles`/`_cur_particles`/`_cur_intermission`.

   **This factoring is the riskiest edit.** Keep the live path's behavior
   byte-identical: when `self.demo is None`, every `_cur_*` returns exactly what
   the old direct `self.sv.X` read returned. Verify with the full test suite.

6. Console commands:
```python
    def _cmd_playdemo(self, args):
        if not args:
            self.con.print("usage: playdemo <name>"); return
        self._play_named_demo(args[0])
    def _play_named_demo(self, name):
        fn = name if name.endswith(".dem") else name + ".dem"
        blob = None
        if fn in self.pak.files:
            blob = self.pak.read(fn)
        elif os.path.exists(fn):
            with open(fn, "rb") as fh: blob = fh.read()
        if blob is None:
            self.con.print(f"playdemo: not found: {fn}"); return
        self.con.active = False
        if not self._load_demo(blob):
            self.con.print(f"playdemo: failed: {fn}")
    def _cmd_stopdemo(self, args):
        if self.demo is not None:
            self.demo = None
            self.con.print("demo stopped")
        # return to a live map so the renderer has a server again
        self._cmd_map(["e1m1"])
```
   Register them in `_register_console`: `playdemo`, `stop`, plus `timedemo`
   (Task 5). Note `_load_demo` already reads from the pak first (the shareware
   demos live there) then the filesystem.

- [ ] **Step 4: Run the tests + full suite**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py` → `OK`.
Run the full suite — live play must be unaffected:
`export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done`
Expected: all `ok`.

- [ ] **Step 5: Smoke-test live play still works**

Run a quick live-play headless check (no demo):
```
PQ_AUDIO=0 python -c "
from client import Client, InputState
c = Client('e1m1'); c.resize(640,480)
for _ in range(30): c.frame(0.05, InputState(move_forward=1.0))
print('live ok', c.demo)"
```
Expected: `live ok None` with no exception.

- [ ] **Step 6: Commit**

```bash
git add client.py tests/test_demo_playback.py
git commit -m "client: demo playback frame path + playdemo/stop; render block reads cl in demo mode

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: `timedemo`

**Files:**
- Modify: `client.py`
- Test: `tests/test_demo_playback.py` (add)

`timedemo <name>` plays a demo as fast as possible (one message per frame) and reports average fps over the run, like `CL_FinishTimeDemo`. The `Demo.timedemo` flag and frame counter are already in Task 4; add the command and the report.

- [ ] **Step 1: Add the failing test**

```python
def test_timedemo_reports_fps():
    from client import Client, InputState
    from quake.pak import Pak
    c = Client.__new__(Client); Client._init_assets_only(c)
    c.resize(640, 480)
    msgs = []
    c.con.print = lambda s: msgs.append(s)     # capture console output
    c._cmd_timedemo(["demo1"])
    # run frames until the demo finishes
    for _ in range(5000):
        c.frame(0.01, InputState())
        if c.demo is None or c.demo.finished:
            break
    assert any("fps" in m.lower() for m in msgs), msgs[-3:]
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py`
Expected: FAIL — `_cmd_timedemo` missing.

- [ ] **Step 3: Implement `timedemo`**

```python
    def _cmd_timedemo(self, args):
        if not args:
            self.con.print("usage: timedemo <name>"); return
        self._play_named_demo(args[0])
        if self.demo is not None:
            self.demo.timedemo = True
    def _finish_timedemo(self):
        import time as _time
        d = self.demo
        if d.start_time and d.frames > 1:
            elapsed = _time.monotonic() - d.start_time
            fps = (d.frames - 1) / elapsed if elapsed > 0 else 0.0
            self.con.print(f"{d.frames-1} frames {elapsed:.1f} seconds {fps:.1f} fps")
        self.con.active = True                  # drop to console with the result
```
Register `timedemo` in `_register_console`. `_finish_timedemo` is already called
from `_demo_frame` when a timedemo run finishes (Task 4 step 3.4).

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py` → `OK`.

- [ ] **Step 5: Commit**

```bash
git add client.py tests/test_demo_playback.py
git commit -m "client: timedemo -- play a demo flat-out and report average fps

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Title demo loop (`start`)

**Files:**
- Modify: `client.py` (`startdemos`/demo-loop state, default construction), `main.py` (default map → `start`)
- Test: `tests/test_demo_playback.py` (add a loop-advance test)

Launching with no map (or `start`) plays `demo1`, then `demo2`, then `demo3`, looping (`CL_NextDemo`/`startdemos`). When a demo finishes (or `stop`), advance to the next demo in the list.

- [ ] **Step 1: Add the failing test**

```python
def test_demo_loop_advances_on_finish():
    from client import Client, InputState
    c = Client("start")                        # title demo loop
    assert c.demo is not None                  # playing demo1 immediately
    first_map = c.mapname
    # force the current demo to finish; next frame should start the next demo
    c.demo.finished = True
    c.frame(0.05, InputState())
    # either a new demo loaded (mapname may differ) or loop wrapped -- demo active
    assert c.demo is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py`
Expected: FAIL — `Client("start")` tries `_load_map("start")` (no such bsp) and raises.

- [ ] **Step 3: Implement the demo loop**

In `client.py`:
1. `__init__`: if `mapname in (None, "start")`, set up the demo loop instead of
   `_load_map`:
   ```python
       self.demo_loop = ["demo1", "demo2", "demo3"]
       self.demo_index = 0
       ...
       if mapname in (None, "start"):
           self._init_assets_only(self)  # already called; ensure no _load_map
           self._register_console(); self.menu = self._build_menu()
           self._next_demo()
       else:
           # existing: _load_map + _register_console + menu
   ```
   (Refactor so `_register_console`/`_build_menu` run in both paths exactly once;
   keep live construction identical.)
2. `_next_demo`:
   ```python
   def _next_demo(self):
       """CL_NextDemo: play the next demo in the title loop, wrapping."""
       if not self.demo_loop:
           return
       name = self.demo_loop[self.demo_index % len(self.demo_loop)]
       self.demo_index += 1
       self._play_named_demo(name)
   ```
3. In `_demo_frame`, when `d.finished` and we are in the title loop (and NOT a
   one-shot `playdemo`), advance: track whether the current demo came from the
   loop (`self.in_demo_loop = True` set by `_next_demo`, cleared by an explicit
   `playdemo`). On finish: `if self.in_demo_loop and not d.timedemo: self._next_demo()`.
4. `main.py`: change the default in `select_frontend` from `"e1m1"` to `"start"`
   so a bare `python main.py` boots the title demo loop. `python main.py e1m1`
   still loads a live map. READ `main.py:797-827` and make the one-line default
   change; confirm `Client("start")` is constructed on that path.

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_demo_playback.py` → `OK`.
Full suite: `export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done` → all `ok`.

- [ ] **Step 5: Commit**

```bash
git add client.py main.py tests/test_demo_playback.py
git commit -m "client: title-screen demo loop (start -> demo1/2/3); main defaults to start

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** `.dem` framing (T1), client-visible surface on `cl` (T2),
  server-free render-stack from signon (T3), demo frame path + `playdemo`/`stop`
  (T4), `timedemo` (T5), title demo loop + `start` default (T6). Covers the design
  spec's Phase 2 deliverables (genuine `demo1/2/3.dem` playback, `timedemo`,
  title loop).
- **Live play untouched:** the demo path is a separate branch; `_cur_*` accessors
  return exactly the old `self.sv.X` values when `self.demo is None`. The full
  Phase-1 test suite is the regression gate at T3, T4, T6.
- **Known carry-overs from Phase 1 (still deferred):** particle trails are the
  Phase-1 stub — demo explosions get a dlight (T2) and `svc_particle` bursts parse,
  but rich rocket/blood trails await the client-side particle migration (tracked).
  `svc_sound` is parsed-and-dropped (audio in demos is silent in Phase 2; wire to
  the mixer in a later pass). PVS: demos DO use PVS culling, so the Phase-1
  `relink` "clear model on absence" must instead gate rendering on
  `msgtime == cl.mtime[0]` — **fix this in T4** (in `SceneFromClient`'s entity
  iterators, skip entities whose `msgtime != cl.mtime[0]` rather than relying on
  model-clearing) so entities that leave/re-enter PVS in a demo don't vanish
  permanently. Verify against `demo1.dem` (which spans rooms).
- **Type consistency:** `Demo`, `_load_demo`, `_init_assets_only`,
  `_load_render_models`, `_render_scene`, `_cur_*`, `_next_demo`,
  `_play_named_demo` referenced consistently across T3–T6. `SceneFromClient`
  surface (T2) matches what `_cur_*`/`_update_dlights`/`_view_model` consume (T4).

## IMPORTANT correction folded into Task 4 (PVS / entity visibility)

Because real demos use PVS culling (entities legitimately leave/re-enter the
view), `SceneFromClient`'s entity iterators (`alias_entities`, `sprite_entities`,
`bsp_model_entities`, `brush_models`, `light_entities`) must render only entities
updated in the current message — gate on `e.msgtime == cl.mtime[0]` — instead of
relying on Phase-1's "clear `e.model` when absent" (which permanently drops an
entity that merely left PVS). Make this change in Task 4 and confirm `demo1.dem`
entities reappear correctly when the camera returns to a room. Keep the live
loopback working (live sends every entity each frame, so `msgtime` always equals
`mtime[0]` there — the gate is a no-op for live).
