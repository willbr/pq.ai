# Phase 3: Demo Recording — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record live single-player play to a genuine protocol-15 `.dem` file that plays back correctly in pq.ai (round-trips through the Phase-2 player). Add `record <name> <map>` and `stop`. Apply the tracked byte-parity fixes to the server's baseline/clientdata emission so recorded demos are structurally faithful to WinQuake.

**Architecture:** The live loopback already builds, every frame, exactly the bytes a `.dem` needs: `write_serverinfo` produces the signon, `build_datagram` produces each frame's `svc_*` datagram. Recording **tees** those — the signon as the demo's first frame, then each frame's datagram with the current view angles — into a `DemoWriter` (`quake/demo.py`). No new serialization; recording is a capture of the stream the server already emits.

**Tech Stack:** Pure Python 3.13 stdlib. Builds on Phases 1–2 (`quake/{msg,protocol,sv_send,cl_parse,demo}.py`, the `client.py` loopback). Tests standalone, `PQ_AUDIO=0`.

**Reference:** `quake-source/WinQuake/cl_demo.c` (`CL_Record_f`, `CL_WriteDemoMessage`, `CL_Stop_f`), `sv_main.c` (`SV_CreateBaseline`, `SV_WriteClientdataToMessage`). Phase-1/2 spec + plans in `docs/superpowers/`.

---

## Scope and honesty about "byte-parity"

The design spec asked for WinQuake byte-parity. Full cross-engine byte-identity (a pq.ai recording playing in real WinQuake) additionally requires **PVS culling** in `write_entities_to_client` and a **complete signon** (all lightstyles, static entities, static sounds, `svc_cdtrack`, player name/frags/colors) — neither of which the Phase-1 loopback implements (it sends every entity each frame and a minimal signon). Porting those is a larger effort than the recording mechanism itself.

**This phase delivers:** faithful recording using the real protocol-15 stream, with the tracked baseline/clientdata byte-parity fixes applied (Task 2), **validated by a record→play round-trip in pq.ai** (Task 4) — exactly the validation the design spec named as gating ("round-trip + structural diff; true cross-engine byte-diff is a spot-check, not gating"). Full WinQuake cross-compatibility (PVS + complete signon) is documented as remaining follow-up, not implemented here.

---

## File structure

| File | Responsibility | Status |
|------|----------------|--------|
| `quake/demo.py` | add `DemoWriter` (CD-track header + append frames via existing `write_demo_frame`) | modify |
| `quake/sv_send.py` | byte-parity: `create_baseline` includes worldspawn + player edicts; `write_clientdata_to_message` folds serverflags into items | modify |
| `client.py` | `record`/`stop` commands; capture signon at record start; tee the per-frame datagram in the live loopback drive | modify |
| `tests/test_demo.py` | `DemoWriter` round-trip with `DemoReader` | modify |
| `tests/test_demo_record.py` | end-to-end: record a scripted session → play it back → trajectories match | create |

---

## Task 1: `DemoWriter`

**Files:**
- Modify: `quake/demo.py`
- Modify: `tests/test_demo.py`

A thin writer: open a file (or buffer), write the CD-track header line, then append framed messages. `write_demo_frame` already exists and is round-trip tested — `DemoWriter` wraps it with file I/O and the header.

- [ ] **Step 1: Add the failing test**

Add to `tests/test_demo.py` (+ `__main__`):

```python
def test_demowriter_roundtrips_through_reader(tmp_path=None):
    import io
    buf = io.BytesIO()
    w = DemoWriter(buf, cdtrack="3")
    w.write_frame((0.0, 90.0, 0.0), b"\x07\x01")
    w.write_frame((1.0, 2.0, 3.0), b"\x08hello\x00")
    w.close()
    r = DemoReader(buf.getvalue())
    assert r.cdtrack == "3"
    a0, m0 = r.next_frame()
    assert m0 == b"\x07\x01" and abs(a0[1] - 90.0) < 1e-6
    a1, m1 = r.next_frame()
    assert m1 == b"\x08hello\x00" and abs(a1[2] - 3.0) < 1e-6
    assert r.next_frame() is None
```

Append `test_demowriter_roundtrips_through_reader()` to `__main__`.

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_demo.py`
Expected: FAIL — `ImportError: cannot import name 'DemoWriter'`.

- [ ] **Step 3: Implement `DemoWriter`**

Add to `quake/demo.py`:

```python
class DemoWriter:
    """Writes a .dem file (CL_Record_f / CL_WriteDemoMessage): the CD-track
    header line, then one framed message per write_frame call. `fp` is any
    binary file-like (an open file or a BytesIO). The caller owns opening it;
    close() flushes and closes it."""

    def __init__(self, fp, cdtrack="0"):
        self.fp = fp
        self.fp.write(str(cdtrack).encode("latin-1") + b"\n")

    def write_frame(self, viewangles, message):
        self.fp.write(write_demo_frame(viewangles, message))

    def close(self):
        self.fp.flush()
        self.fp.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_demo.py` → `OK`.

- [ ] **Step 5: Commit**

```bash
git add quake/demo.py tests/test_demo.py
git commit -m "demo: DemoWriter -- CD-track header + framed message append

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Byte-parity baseline + clientdata fixes

**Files:**
- Modify: `quake/sv_send.py`
- Modify: `tests/test_sv_send.py`

Apply the two tracked byte-parity items so recorded demos match WinQuake's signon/clientdata structure. **Both must keep the existing loopback round-trip green** (the parser is symmetric).

READ `quake-source/WinQuake/sv_main.c` `SV_CreateBaseline` (~925) and `SV_WriteClientdataToMessage` (~576) first, and `quake/sv.py` for the serverflags accessor and the player-edict / maxclients notion.

**Fix A — `create_baseline` includes worldspawn + player edicts (`sv_main.c:925`):**
- Start the loop at edict **0** (worldspawn), not 1 — the world gets a baseline (its `modelindex` is 1, the level `.bsp`).
- For client edicts `1..maxclients` (single-player: just the player edict `sv.player`), force `baseline.colormap = entnum` and `baseline.modelindex = sv.model_index("progs/player.mdl")`, matching the C. (The player's VM `.modelindex`/`.colormap` may be 0 in this port's hand-built client edict; the C always forces these for players.)

**Fix B — `write_clientdata_to_message` folds serverflags into items (`sv_main.c`):**
- WinQuake writes `items | ((serverflags & 15) << 28)` where `serverflags` is the episode-sigil bitfield. Find the serverflags value on the server (`sv.serverflags`) and fold it: `items = base_items | ((int(sv.serverflags) & 0x0f) << 28)` before writing the items long. This makes `cl.items` carry sigil bits, matching `Server.hud_status()` (which already folds them via `decode_hud_items`).

- [ ] **Step 1: Add failing tests**

Add to `tests/test_sv_send.py` (+ `__main__`):

```python
def test_baseline_includes_worldspawn_and_player():
    sv = _boot()
    sv.create_baseline()
    assert 0 in sv.baselines, "worldspawn (edict 0) must have a baseline"
    assert sv.baselines[0].modelindex == 1, "world modelindex is 1 (the .bsp)"
    # the player edict baseline forces the player model + colormap=entnum
    p = sv.player
    assert sv.baselines[p].colormap == p
    assert sv.baselines[p].modelindex == sv.model_index("progs/player.mdl")


def test_clientdata_folds_serverflags_into_items():
    from quake.msg import MsgWriter, MsgReader
    from quake.sv_send import write_clientdata_to_message
    from quake.cl_parse import ClientState
    sv = _boot()
    sv.serverflags = 3                     # two sigils
    w = MsgWriter(); write_clientdata_to_message(sv, w)
    r = MsgReader(bytes(w.data)); assert r.byte() == 15   # svc_clientdata
    cl = ClientState(); cl.parse_clientdata(r)
    assert (cl.items >> 28) & 0x0f == 3, "serverflags must ride in items high bits"
```

- [ ] **Step 2: Run to verify they fail**

Run: `PQ_AUDIO=0 python tests/test_sv_send.py`
Expected: FAIL — worldspawn not in baselines / serverflags not folded.

- [ ] **Step 3: Implement Fix A and Fix B in `quake/sv_send.py`**

In `create_baseline`, change the loop to start at 0 and special-case players:
```python
def create_baseline(sv):
    vm, f = sv.vm, sv.f
    sv.baselines = {}
    maxclients = getattr(sv, "maxclients", 1)
    for e in range(0, vm.num_edicts):          # include edict 0 (worldspawn)
        if e != 0 and vm.free[e]:
            continue
        mi = int(vm.fget_i(e, f["modelindex"]))
        frame = int(vm.fget_f(e, f["frame"]))
        colormap = int(vm.fget_f(e, f["colormap"]))
        skin = int(vm.fget_f(e, f["skin"]))
        origin = tuple(vm.fget_v(e, f["origin"]))
        angles = tuple(vm.fget_v(e, f["angles"]))
        if 1 <= e <= maxclients:               # player edicts: SV_CreateBaseline
            colormap = e
            mi = sv.model_index("progs/player.mdl")
        sv.baselines[e] = Baseline(modelindex=mi, frame=frame, colormap=colormap,
                                   skin=skin, origin=origin, angles=angles)
```
(Confirm `sv.maxclients` exists; if not, default to 1 as shown. Confirm the world
edict 0 is never in `vm.free` / is always "live"; the `e != 0` guard skips the
free-check for the world.)

In `write_clientdata_to_message`, fold serverflags into the items long:
```python
    items = int(vm.fget_f(e, f["items"]))
    items |= (int(getattr(sv, "serverflags", 0)) & 0x0f) << 28   # episode sigils
```
(Apply this where `items` is computed, before `w.long(items)`. Confirm
`sv.serverflags` is the right attribute by reading sv.py.)

- [ ] **Step 4: Run the tests + the loopback regression**

Run: `PQ_AUDIO=0 python tests/test_sv_send.py` → `OK`.
Run the FULL suite — the loopback and live play must stay green (the parser is
symmetric, and the worldspawn baseline / player-model change affects what the
client sees):
`export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done`
Expected: all `ok`. **If a live test regresses** (e.g. the world now appears as a
rendered entity, or the player body renders because it now has a baseline model),
investigate: the renderer must still not draw the world as an alias entity (it's
the BSP) nor the player's own body (viewentity skip). The `SceneFromClient`
iterators skip the world (`model.startswith("*")` excludes `maps/*.bsp`) and the
viewentity — confirm those guards still hold with the new baselines. Fix the guard,
not the baseline, if needed.

- [ ] **Step 5: Commit**

```bash
git add quake/sv_send.py tests/test_sv_send.py
git commit -m "sv_send: byte-parity -- worldspawn+player baselines, serverflags in items

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Recording integration (`record`/`stop`)

**Files:**
- Modify: `client.py`
- Test: `tests/test_demo_record.py` (created in Task 4)

Add `record <name> <map>` (load the map live, open a `DemoWriter`, write the signon as the first frame, start teeing), tee each live frame's datagram, and extend `stop` to finish a recording.

READ `client.py`: `_load_map` (the signon build at ~424-428), the live loopback drive (~1573-1577), `_cmd_map` (~the map command), `_cmd_stopdemo` (the `stop` command), and `_register_console`.

- [ ] **Step 1: Implement recording**

1. Initialise `self.recording = None` in `_init_assets_only`.

2. `record` command:
```python
    def _cmd_record(self, args):
        if len(args) < 1:
            self.con.print("usage: record <name> [map]"); return
        name = args[0]
        mapname = args[1] if len(args) > 1 else self.mapname
        # start a fresh game on the map (Host_Record_f loads the level), like
        # the live `map` command -- this rebuilds sv + cl + the signon.
        self.spawn_parms = None; self.serverflags = 0.0
        if not self._load_map(mapname):
            self.con.print(f"record: no such map: {mapname}"); return
        path = name if name.endswith(".dem") else name + ".dem"
        path = os.path.join(os.path.dirname(PAK_PATH), path)
        try:
            fp = open(path, "wb")
        except OSError as e:
            self.con.print(f"record: {e}"); return
        from quake.demo import DemoWriter
        self.recording = DemoWriter(fp, cdtrack="0")
        # write the signon as the demo's first frame (rebuild the bytes that
        # _load_map already parsed into cl; create_baseline ran in _load_map)
        sw = MsgWriter(); write_serverinfo(self.sv, sw)
        ang = (self.pitch, self.yaw, 0.0)
        self.recording.write_frame(ang, bytes(sw.data))
        self.con.print(f"recording to {path}")
```

3. Tee each frame: in the live loopback drive (after `build_datagram(self.sv, dg)`,
   ~line 1574), append the datagram to the recording with the current view angles:
```python
        dg = MsgWriter()
        build_datagram(self.sv, dg)
        if self.recording is not None:
            self.recording.write_frame((self.pitch, self.yaw, 0.0), bytes(dg.data))
        self.cl.time = self.sv.time
        self.cl.parse_message(MsgReader(dg.data))
        self.cl.relink(dt)
```
   (Recording the *base* aim angles `(pitch, yaw, 0)`; roll and punch ride in the
   datagram's clientdata, reproduced on playback. This is the live path, so
   `self.pitch`/`self.yaw` are this frame's input angles.)

4. Extend `stop` to finish a recording. In `_cmd_stopdemo` (the `stop` command),
   handle recording first:
```python
    def _cmd_stopdemo(self, args):
        if self.recording is not None:
            self.recording.close()
            self.recording = None
            self.con.print("stopped recording")
            return
        # ... existing playback-stop behavior ...
```

5. Register `record` in `_register_console` (`stop` is already registered).

- [ ] **Step 2: Smoke test recording**

Run a headless record smoke (no test file yet — that's Task 4):
```
PQ_AUDIO=0 python -c "
from client import Client, InputState
import os
c = Client('e1m1'); c.resize(640,480)
c._cmd_record(['unittest_rec','e1m1'])
for _ in range(60): c.frame(0.05, InputState(move_forward=1.0))
c._cmd_stopdemo([])
p = os.path.join('quake-shareware/id1','unittest_rec.dem')
print('recorded', os.path.getsize(p), 'bytes'); os.remove(p)
"
```
Expected: prints a non-trivial byte count, no exception.

- [ ] **Step 3: Run the full suite (live regression)**

`export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done`
Expected: all `ok` (the tee is a no-op when `self.recording is None`).

- [ ] **Step 4: Commit**

```bash
git add client.py
git commit -m "client: record/stop -- tee the live datagram + signon to a .dem

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Record → play round-trip test (the validation gate)

**Files:**
- Create: `tests/test_demo_record.py`

The design spec's gating validation: record a scripted session, play it back, and confirm the playback reproduces the recorded session (camera trajectory + entity presence). This proves recording and playback are faithful mirrors end to end.

- [ ] **Step 1: Write the round-trip test**

```python
# tests/test_demo_record.py
"""Record -> play round-trip: record a scripted e1m1 session to a temp .dem,
play it back, and assert the camera trajectory and entity presence reproduce.
Run: PQ_AUDIO=0 python tests/test_demo_record.py."""
import _bootstrap  # noqa: F401
import os
import tempfile
from client import Client, InputState


def test_record_then_play_reproduces_camera_path():
    rec_dir = tempfile.mkdtemp()
    name = os.path.join(rec_dir, "rt")
    # --- record ---
    c = Client("e1m1"); c.resize(640, 480)
    c._cmd_record([name, "e1m1"])
    recorded_pos = []
    for _ in range(80):
        c.frame(0.05, InputState(move_forward=1.0))
        recorded_pos.append(tuple(round(v, 0) for v in c.pos))
    c._cmd_stopdemo([])
    demo_path = name + ".dem"
    assert os.path.getsize(demo_path) > 0

    # --- play back ---
    p = Client.__new__(Client); Client._init_assets_only(p)
    if getattr(p, "menu", None) is None:
        p._finish_construction() if hasattr(p, "_finish_construction") else None
    with open(demo_path, "rb") as fh:
        p._load_demo(fh.read())
    p.resize(640, 480)
    played_pos = []
    for _ in range(80):
        if p.demo.finished:
            break
        p.frame(0.05, InputState())
        played_pos.append(tuple(round(v, 0) for v in p.pos))

    os.remove(demo_path); os.rmdir(rec_dir)

    # the played-back camera must trace (close to) the recorded path. Allow a
    # small tolerance for coord quantization (1/8 unit) and one-frame interp lag.
    assert len(played_pos) >= 60, f"playback too short: {len(played_pos)}"
    # compare a mid-run sample: playback origin near a recorded origin
    rx, ry, rz = recorded_pos[50]
    near = min(abs(px - rx) + abs(py - ry) + abs(pz - rz)
               for (px, py, pz) in played_pos)
    assert near < 8.0, f"playback path diverged from recording: {near}"


if __name__ == "__main__":
    test_record_then_play_reproduces_camera_path()
    print("OK")
```

(If the exact tolerance/indices need adjustment against real data, tune them to a
genuine match — do NOT weaken to vacuity. The intent: the played path must clearly
follow the recorded path, not merely "not crash".)

- [ ] **Step 2: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_demo_record.py` → `OK`.
If it fails because the player-baseline change (Task 2) makes the player render or
the camera diverge, debug against the actual data — the recorded datagram drives
the playback camera via `cl.entities[cl.viewentity]`, so the player edict must be
sent in the datagram (it now has a baseline model from Task 2 — confirm the
viewentity skip in `SceneFromClient` still prevents drawing the own body while the
camera still reads its origin).

- [ ] **Step 3: Full suite**

`export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done`
Expected: all `ok`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_demo_record.py
git commit -m "test: record -> play round-trip reproduces the camera path

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** `DemoWriter` (T1), byte-parity baseline/clientdata fixes (T2,
  closing tracked tasks 11), `record`/`stop` teeing the live stream (T3), and the
  record→play round-trip validation the spec named as gating (T4).
- **Honesty:** full WinQuake cross-engine byte-parity (PVS culling + complete
  signon: lightstyles, statics, static sounds, cdtrack, player name/frags/colors)
  is **not** delivered here and is documented as remaining follow-up. This phase
  delivers faithful in-engine round-trip recording with the baseline byte-parity
  fixes — the spec's stated gating validation.
- **Live safety:** the per-frame tee is a no-op when `self.recording is None`; the
  full suite is the regression gate at T2, T3, T4. The Task-2 baseline change is the
  one with live-render risk (world/player now have baselines) — the viewentity and
  world-model guards in `SceneFromClient` must still hold; verified by the suite +
  the round-trip.
- **Type consistency:** `DemoWriter`/`write_frame`/`close`, `self.recording`,
  `_cmd_record`, the extended `_cmd_stopdemo` referenced consistently across T1–T4.

## Potential follow-up (not in this phase)
Cross-engine WinQuake parity: port PVS culling into `write_entities_to_client` and
complete the signon (`svc_lightstyle` for all styles, `svc_spawnstatic`,
`svc_spawnstaticsound`, `svc_cdtrack`, `svc_updatename`/`frags`/`colors`). Then a
pq.ai recording could be diffed against / played in real WinQuake.
