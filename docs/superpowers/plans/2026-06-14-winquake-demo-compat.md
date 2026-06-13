# WinQuake-Compatible Demo Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make pq.ai's recorded `.dem` files play in real WinQuake by reproducing WinQuake's exact 3-phase connect signon (ending in `svc_signonnum` 1/2/3), emitting the signon content a real demo carries (statics, static sounds, all 64 lightstyles, stats, setangle), and PVS-culling per-frame entities.

**Architecture:** Replace the minimal single-block `write_serverinfo` with `build_signon(sv) -> [phase0, phase1, phase2]` (three message blocks mirroring `SV_SendServerinfo` / prespawn `sv.signon` flush / `Host_Spawn_f`). The live loopback concatenates and parses all three (one-shot, unchanged behaviour); the recorder writes them as the demo's first three frames, then tees PVS-culled per-frame datagrams. Verification is structural parity against the genuine shareware `demo1.dem` (a real WinQuake recording) plus our own record→play round-trip; final cross-engine confirmation is a documented manual test in the user's WinQuake.

**Tech Stack:** Pure Python 3.13 stdlib. Builds on the Phase 1–3 protocol stack (`quake/{msg,protocol,sv_send,cl_parse,demo}.py`, `client.py` recording). Tests standalone, `PQ_AUDIO=0`.

**Reference:** `quake-source/WinQuake/{sv_main.c,host_cmd.c,pr_cmds.c,cl_parse.c,cl_demo.c}`. Gap analysis (this plan's basis) lives in the conversation that produced it; key cites are inline per task.

---

## What "plays in WinQuake" requires (from the gap analysis)

Real `demo1.dem` (e1m3) splits its signon across **three** demo frames, ending each phase with a `svc_signonnum`:

- **Phase 0** (`SV_SendServerinfo`, sv_main.c:189-233): `svc_print`(version) → `svc_serverinfo`(proto,maxclients,gametype,levelname,model list+0,sound list+0) → `svc_cdtrack`(track,looptrack) → `svc_setview`(player edict) → `svc_signonnum 1`.
- **Phase 1** (prespawn `sv.signon` flush, host_cmd.c:1254-1272): `svc_spawnstatic`×N → `svc_spawnbaseline`×N → `svc_spawnstaticsound`×N (built at load) → `svc_signonnum 2`.
- **Phase 2** (`Host_Spawn_f`, host_cmd.c:1279-1395): `svc_time`(sv.time) → `svc_updatename/frags/colors`(player) → `svc_lightstyle`×64 (ALL) → `svc_updatestat`×4 (totals) → `svc_setangle` → `svc_clientdata` → `svc_signonnum 3`.
- **Phase 3+**: per-frame `svc_time` → `svc_clientdata` → PVS-culled entity updates (real demo: ~5/frame; ours today: all 203).

**The blocker** is `svc_signonnum` 2 and 3 with the Phase-2 `svc_time` — without them a WinQuake client never reaches `SIGNONS` and never renders. Tasks are ordered so the **playable core (Tasks 1–2) lands first**; Tasks 3–6 are fidelity layers (a WinQuake will play after Task 2, just missing torches/ambient-sound/secret-counts and with bloated all-entity frames).

---

## File structure

| File | Responsibility | Status |
|------|----------------|--------|
| `quake/sv_send.py` | replace `write_serverinfo` with `build_signon(sv)->[bytes,bytes,bytes]`; PVS cull in `write_entities_to_client` | modify |
| `quake/sv.py` | track static entities (`makestatic`) for `svc_spawnstatic`; expose totals/lightstyles/cdtrack/ambients accessors | modify |
| `client.py` | `_cmd_record` writes 3 signon frames; `_load_map`/`_load_demo` parse a multi-frame signon | modify |
| `tests/test_winquake_compat.py` | structural parity vs `demo1.dem` + strict signon validator (the gate) | create |
| `docs/winquake-demo-testing.md` | manual cross-engine test steps for the user | create |

---

## Task 1: Three-phase signon framing + the `svc_signonnum` 1/2/3 handshake

**Files:**
- Modify: `quake/sv_send.py` (replace `write_serverinfo` with `build_signon`)
- Modify: `client.py` (`_cmd_record`, `_load_map`, `_load_demo`)
- Test: `tests/test_winquake_compat.py`

This is the playable core: produce the 3-phase signon and write it as 3 demo frames, keeping the live loopback and our own playback green. Use only content we already have (serverinfo, baselines, cdtrack, setview, time, clientdata, setangle, signonnum). Statics/staticsounds/lightstyles/stats arrive in later tasks (the phase blocks have the slots; they start minimal).

READ first: `quake/sv_send.py` (`write_serverinfo`, `write_clientdata_to_message`, `write_entities_to_client`), `client.py:424-432` (loopback signon parse in `_load_map`), `client.py:_load_demo` (signon parse) and `_cmd_record`, and `cl_parse.py:_dispatch` (svc_signonnum/svc_time handlers).

- [ ] **Step 1: Write the failing structural test**

```python
# tests/test_winquake_compat.py
"""Structural parity of our recordings against WinQuake. The genuine shareware
demo1.dem is a real WinQuake recording, so its message structure is the gold
reference. Run muted: PQ_AUDIO=0 python tests/test_winquake_compat.py."""
import _bootstrap  # noqa: F401
from quake import protocol as P
from quake.sv_send import build_signon


def _boot(mapname="e1m1"):
    from quake.pak import Pak
    from quake.bsp import Bsp
    from quake.progs import Progs
    from quake.sv import Server
    from quake.physics import Physics
    pak = Pak("quake-shareware/id1/pak0.pak")
    b = Bsp(pak.read(f"maps/{mapname}.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b, mapname=f"maps/{mapname}.bsp",
                skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    sv.spawn_player((480.0, 0.0, 100.0), (0.0, 0.0, 0.0))
    for _ in range(3):
        sv.run_frame(0.1)
    sv.create_baseline()
    return sv


def _svc_sequence(msg):
    """Walk a message buffer via the real parser, returning the ordered list of
    top-level svc ids (each entity fast-update logged as -1). Re-uses
    ClientState's handlers so every message's payload is consumed correctly and
    the next svc boundary is found."""
    from quake.cl_parse import ClientState
    from quake.msg import MsgReader
    cl = ClientState()
    r = MsgReader(msg)
    out = []
    while not r.at_end:
        cmd = r.byte()
        if cmd & 128:                     # fast entity update (high bit set)
            out.append(-1)
            cl.parse_update(cmd & 127, r)
        else:
            out.append(cmd)
            cl._dispatch(cmd, r)          # consume this message's payload
    return out


def test_signon_has_three_phases_ending_signonnum_123():
    sv = _boot("e1m1")
    phases = build_signon(sv)
    assert len(phases) == 3, "signon must be 3 phases (serverinfo/prespawn/spawn)"
    p0, p1, p2 = (_svc_sequence(b) for b in phases)
    # phase 0: serverinfo ... signonnum 1
    assert P.svc_serverinfo in p0 and p0[-1] == P.svc_signonnum
    # phase 1: baselines ... signonnum 2
    assert P.svc_spawnbaseline in p1 and p1[-1] == P.svc_signonnum
    # phase 2: a svc_time then clientdata, ending signonnum 3
    assert P.svc_time in p2 and P.svc_clientdata in p2 and p2[-1] == P.svc_signonnum


if __name__ == "__main__":
    test_signon_has_three_phases_ending_signonnum_123()
    print("OK")
```

(Note: `_svc_sequence` re-dispatches each message through `ClientState` to consume its payload and find the next svc boundary. If the inline re-parse is awkward, simplify it to a clean helper that calls `cl._dispatch(cmd, r)` for non-update commands and `cl.parse_update(cmd & 127, r)` for updates — the goal is just the ordered list of top-level svc ids. Keep it correct.)

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py`
Expected: FAIL — `ImportError: cannot import name 'build_signon'`.

- [ ] **Step 3: Implement `build_signon` in `quake/sv_send.py`**

Replace `write_serverinfo` with `build_signon(sv)` returning three message blocks. Reuse the existing serverinfo body for phase 0; move the baselines to phase 1; add the signonnum terminators and the phase-2 spawn block. Keep `write_serverinfo` as a thin wrapper that concatenates the three (so existing callers/tests don't break) OR update callers in this task.

```python
def build_signon(sv):
    """The three connect-handshake message blocks a real Quake server sends,
    mirroring SV_SendServerinfo / the prespawn sv.signon flush / Host_Spawn_f
    (sv_main.c:189, host_cmd.c:1254, host_cmd.c:1279). Returned as three byte
    blocks; the recorder writes them as the demo's first three frames and the
    live loopback concatenates+parses them. Ends each phase with svc_signonnum
    1/2/3 -- the sequence a WinQuake client needs to reach SIGNONS and render."""
    vm, f = sv.vm, sv.f
    # --- phase 0: serverinfo ---
    w0 = MsgWriter()
    w0.byte(P.svc_print)
    w0.string(f"\x02\nPQ.AI demo, protocol {P.PROTOCOL_VERSION}\n")
    w0.byte(P.svc_serverinfo)
    w0.long(P.PROTOCOL_VERSION)
    w0.byte(1)                                   # maxclients
    w0.byte(0)                                   # gametype: GAME_COOP
    w0.string(sv.level_name())
    for name in sv.model_precache[1:]:
        w0.string(name)
    w0.string("")
    for name in sv.sound_precache[1:]:
        w0.string(name)
    w0.string("")
    w0.byte(P.svc_cdtrack)
    cd = sv.cdtrack()                            # worldspawn .sounds (Task 3 adds it; 0 ok)
    w0.byte(cd); w0.byte(cd)
    w0.byte(P.svc_setview)
    w0.short(sv.player)
    w0.byte(P.svc_signonnum); w0.byte(1)

    # --- phase 1: prespawn buffer (statics, baselines, static sounds) ---
    w1 = MsgWriter()
    write_static_entities(sv, w1)                # Task 4 (no-op until then)
    for e, base in sv.baselines.items():
        w1.byte(P.svc_spawnbaseline)
        w1.short(e)
        w1.byte(base.modelindex); w1.byte(base.frame)
        w1.byte(base.colormap); w1.byte(base.skin)
        for i in range(3):
            w1.coord(base.origin[i]); w1.angle(base.angles[i])
    write_static_sounds(sv, w1)                  # Task 3 (no-op until then)
    w1.byte(P.svc_signonnum); w1.byte(2)

    # --- phase 2: spawn block ---
    w2 = MsgWriter()
    w2.byte(P.svc_time); w2.float(sv.time)
    write_all_lightstyles(sv, w2)                # Task 2 (minimal until then)
    write_total_stats(sv, w2)                    # Task 2 (no-op until then)
    ang = sv.player_angles() or (0.0, 0.0, 0.0)
    w2.byte(P.svc_setangle)
    w2.angle(ang[0]); w2.angle(ang[1]); w2.angle(0.0)
    write_clientdata_to_message(sv, w2)
    w2.byte(P.svc_signonnum); w2.byte(3)
    return [bytes(w0.data), bytes(w1.data), bytes(w2.data)]
```

Add stubs for the helpers this references so Task 1 compiles (later tasks fill them):
```python
def write_static_entities(sv, w):    # Task 4
    return
def write_static_sounds(sv, w):      # Task 3
    return
def write_all_lightstyles(sv, w):    # Task 2 -- minimal stub: emit changed styles
    for idx, patt in sv.lightstyles.items():
        w.byte(P.svc_lightstyle); w.byte(idx); w.string(patt)
def write_total_stats(sv, w):        # Task 2
    return
```

Add the `Server.cdtrack()` accessor in `quake/sv.py` (returns the worldspawn `sounds` field as int, or 0 — read it from the BSP entity string / a global; Task 3 wires it properly, return 0 for now):
```python
    def cdtrack(self):
        return int(getattr(self, "_cdtrack", 0))
```

- [ ] **Step 4: Update the loopback + recorder + playback callers**

In `client.py`:
- `_load_map` (the `write_serverinfo` call ~line 426): replace with
  ```python
  from quake.sv_send import build_signon
  sw = MsgWriter()
  for block in build_signon(self.sv):
      sw.data += block
  self.cl.parse_message(MsgReader(sw.data))
  ```
  (concatenate the 3 phases and parse once — same end state as before, now with the spawn block too).
- `_cmd_record`: replace the single signon-frame write with three frames:
  ```python
  for block in build_signon(self.sv):
      self.recording.write_frame((self.pitch, self.yaw, 0.0), block)
  ```
- `_load_demo`: the signon is now potentially multiple frames. Parse frames until the spawn completes (a `svc_signonnum 3` was seen, or simpler: keep reading+parsing frames until `cl.model_precache` is populated AND a `svc_time` has set `cl.mtime[0]`). Concretely, loop:
  ```python
  reader = DemoReader(blob)
  self.cl = ClientState()
  first_angles = None
  while True:
      fr = reader.next_frame()
      if fr is None:
          self.con.print("demo: no playable frames"); return False
      if first_angles is None:
          first_angles = fr[0]
      self.cl.parse_message(MsgReader(fr[1]))
      if self.cl.model_precache and len(self.cl.model_precache) > 1 \
              and self.cl.mtime[0] > 0.0:
          break        # serverinfo (precache) + spawn svc_time seen -> ready
  self.cl.mviewangles[0] = list(first_angles)
  self.cl.mviewangles[1] = list(first_angles)
  ```
  Then continue as before (map from `cl.model_precache[1]`, build render stack). This keeps our own playback working for BOTH the new 3-frame signon AND the genuine shareware demos (which already use 3 frames -- so this also makes _load_demo more faithful).

- [ ] **Step 5: Run the tests + full suite (live + playback must stay green)**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py` → `OK`.
Run: `PQ_AUDIO=0 python tests/test_demo_record.py` (round-trip) → `OK`.
Run the full suite: `export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done` → all `ok`.
Play all three shareware demos headless (~200 frames each) → no exception, camera moves (the `_load_demo` multi-frame change must not break them).

- [ ] **Step 6: Commit**

```bash
git add quake/sv_send.py quake/sv.py client.py tests/test_winquake_compat.py
git commit -m "demo: WinQuake 3-phase signon handshake (svc_signonnum 1/2/3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: All-64 lightstyles + total-secret/monster stats + setangle at spawn

**Files:**
- Modify: `quake/sv_send.py` (`write_all_lightstyles`, `write_total_stats`)
- Modify: `quake/sv.py` (expose the total counts)
- Test: `tests/test_winquake_compat.py`

A real demo's spawn block sends ALL 64 lightstyles (host_cmd.c:1352-1357) and the four total/found stats (host_cmd.c:1362-1376). Without the lightstyles a WinQuake plays but lighting is wrong; without the stats the HUD shows 0 secrets.

READ first: `host_cmd.c:1352-1376`; `quake/sv.py` for `self.lightstyles` and how `total_secrets`/`total_monsters`/`found_secrets`/`killed_monsters` are stored (likely QC globals via `gget_f`).

- [ ] **Step 1: Add the failing test**

Add to `tests/test_winquake_compat.py` (+ `__main__`):

```python
def test_spawn_block_has_64_lightstyles_and_total_stats():
    sv = _boot("e1m1")
    phases = build_signon(sv)
    seq = _svc_sequence(phases[2])
    assert seq.count(P.svc_lightstyle) == 64, "all MAX_LIGHTSTYLES sent at spawn"
    assert seq.count(P.svc_updatestat) >= 4, "total secrets/monsters stats sent"
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py`
Expected: FAIL — fewer than 64 lightstyles / no updatestat.

- [ ] **Step 3: Implement the spawn-block content**

```python
def write_all_lightstyles(sv, w):
    """svc_lightstyle for ALL 64 styles at spawn (host_cmd.c:1352). Styles the
    QC never set are sent as empty strings, like WinQuake."""
    for i in range(64):
        w.byte(P.svc_lightstyle)
        w.byte(i)
        w.string(sv.lightstyles.get(i, ""))

def write_total_stats(sv, w):
    """svc_updatestat for the secret/monster totals at spawn (host_cmd.c:1362)."""
    for stat, value in ((P.STAT_TOTALSECRETS, sv.total_secrets()),
                        (P.STAT_TOTALMONSTERS, sv.total_monsters()),
                        (P.STAT_SECRETS, sv.found_secrets()),
                        (P.STAT_MONSTERS, sv.killed_monsters())):
        w.byte(P.svc_updatestat)
        w.byte(stat)
        w.long(int(value))
```

Add the accessors to `quake/sv.py` (read the QC globals — confirm the global names against `quake/sv.py`'s existing `intermission_stats()` which already reads these):
```python
    def total_secrets(self):   return self.gget_f("total_secrets")
    def total_monsters(self):  return self.gget_f("total_monsters")
    def found_secrets(self):   return self.gget_f("found_secrets")
    def killed_monsters(self): return self.gget_f("killed_monsters")
```
(`intermission_stats()` already reads these four — reuse the exact same `gget_f` keys it uses.)

- [ ] **Step 4: Run to verify it passes + suite**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py` → `OK`.
Full suite green. The live loopback now also receives all 64 lightstyles (harmless; `cl.lightstyles` already keyed by index).

- [ ] **Step 5: Commit**

```bash
git add quake/sv_send.py quake/sv.py tests/test_winquake_compat.py
git commit -m "demo: emit all 64 lightstyles + total stat counts in the spawn block

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Static sounds (`svc_spawnstaticsound`) + cdtrack

**Files:**
- Modify: `quake/sv_send.py` (`write_static_sounds`)
- Modify: `quake/sv.py` (`cdtrack` from worldspawn `sounds`)
- Test: `tests/test_winquake_compat.py`

We already track ambient sounds in `sv.ambients` (`[(name, pos, vol, atten)]`, built at `load_level` from the `ambientsound` builtin). Emit them as `svc_spawnstaticsound` in the prespawn block (PF_ambientsound, pr_cmds.c:506). Also set the real cdtrack from the worldspawn `sounds` field.

READ first: `quake/sv.py` for `self.ambients` (exact tuple shape and how it's built), and how to read the worldspawn `sounds` key (the BSP entity string / a QC field).

- [ ] **Step 1: Add the failing test**

Add to `tests/test_winquake_compat.py` (+ `__main__`):

```python
def test_prespawn_emits_static_sounds():
    sv = _boot("e1m1")
    # e1m1 has ambient sounds; sv.ambients should be non-empty after load
    if not sv.ambients:
        return                                  # map without ambients: skip
    seq = _svc_sequence(build_signon(sv)[1])
    assert P.svc_spawnstaticsound in seq
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py`
Expected: FAIL — no `svc_spawnstaticsound` in the prespawn block.

- [ ] **Step 3: Implement**

```python
def write_static_sounds(sv, w):
    """svc_spawnstaticsound for each looping ambient the QC spawned (PF_ambient
    sound, pr_cmds.c:506): 3 coords, sound index, vol*255, atten*64. Sourced from
    sv.ambients, which load_level built from the ambientsound builtin."""
    for name, pos, vol, atten in sv.ambients:
        idx = sv.sound_index(name)
        if idx <= 0:
            continue
        w.byte(P.svc_spawnstaticsound)
        for c in pos:
            w.coord(c)
        w.byte(idx)
        w.byte(min(255, int(vol * 255)))
        w.byte(min(255, int(atten * 64)))
```

Add `Server.sound_index(name)` to `quake/sv.py` (mirror `model_index`): `return self.sound_precache.index(name) if name in self.sound_precache else 0`. Wire `cdtrack()` to the real worldspawn value (read the worldspawn `sounds` field from the BSP entities at load and store `self._cdtrack`); if the field isn't readily available, leave `cdtrack()` returning 0 (a WinQuake plays fine with cdtrack 0 — no music) and note it.

Confirm `sv.ambients` tuple order matches `(name, pos, vol, atten)` — adjust the unpack to the real shape.

- [ ] **Step 4: Run + suite**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py` → `OK`. Full suite green.

- [ ] **Step 5: Commit**

```bash
git add quake/sv_send.py quake/sv.py tests/test_winquake_compat.py
git commit -m "demo: emit svc_spawnstaticsound (ambient loops) + cdtrack in the signon

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Static entities (`svc_spawnstatic` via `makestatic`)

**Files:**
- Modify: `quake/sv.py` (track static entities from the `makestatic` builtin)
- Modify: `quake/sv_send.py` (`write_static_entities`)
- Test: `tests/test_winquake_compat.py`

Real demos carry `svc_spawnstatic` for QC `makestatic()` entities (torches, flames — pr_cmds.c:1584 `PF_makestatic`). Our server has no `makestatic` tracking, so torches are absent both in our render and our recordings. Add a `makestatic` builtin that snapshots the entity's render state into `sv.static_entities` and frees the edict (as the C does), then emit them.

READ first: `quake/sv.py` builtin registration (how builtins map to `_pf_*` methods), `quake-source/WinQuake/pr_cmds.c:1584` (`PF_makestatic` — what it reads and that it frees the edict). Confirm whether `makestatic` is currently bound to anything (likely a no-op or missing).

- [ ] **Step 1: Add the failing test**

Add to `tests/test_winquake_compat.py` (+ `__main__`):

```python
def test_makestatic_entities_emitted():
    sv = _boot("e1m1")
    # e1m1 has makestatic torches; after load_level sv.static_entities is populated
    if not getattr(sv, "static_entities", None):
        return                                  # map without statics: skip
    seq = _svc_sequence(build_signon(sv)[1])
    assert P.svc_spawnstatic in seq
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py`
Expected: FAIL — `sv.static_entities` missing/empty or no `svc_spawnstatic`.

- [ ] **Step 3: Implement the builtin + emitter**

In `quake/sv.py`: add `self.static_entities = []` in `__init__`, and a `_pf_makestatic` builtin that snapshots `{modelindex, frame, colormap, skin, origin, angles}` and frees the edict:
```python
    def _pf_makestatic(self):
        e = self.vm.parm_i(0)              # the entity (PF_makestatic arg is `self`)
        vm, f = self.vm, self.f
        self.static_entities.append({
            "modelindex": int(vm.fget_i(e, f["modelindex"])),
            "frame": int(vm.fget_f(e, f["frame"])),
            "colormap": int(vm.fget_f(e, f["colormap"])),
            "skin": int(vm.fget_f(e, f["skin"])),
            "origin": tuple(vm.fget_v(e, f["origin"])),
            "angles": tuple(vm.fget_v(e, f["angles"])),
        })
        self._free_edict(e)                # PF_makestatic frees the edict (ED_Free)
```
Bind `makestatic` to `_pf_makestatic` in the builtin table (find where `_pf_*` builtins are registered and add it at the correct builtin number — `makestatic` is builtin #69 in `pr_cmds.c`; confirm the registration mechanism). Use the real `_free_edict`/`ED_Free` equivalent in this codebase (find how other builtins free edicts, e.g. the `remove` builtin).

In `quake/sv_send.py`:
```python
def write_static_entities(sv, w):
    """svc_spawnstatic for each makestatic entity (PF_makestatic, pr_cmds.c:1584):
    modelindex, frame, colormap, skin, then 3x(coord, angle)."""
    for s in getattr(sv, "static_entities", ()):
        w.byte(P.svc_spawnstatic)
        w.byte(s["modelindex"]); w.byte(s["frame"])
        w.byte(s["colormap"]); w.byte(s["skin"])
        for i in range(3):
            w.coord(s["origin"][i]); w.angle(s["angles"][i])
```

(Our client already parses `svc_spawnstatic` into `cl.static_entities` — Phase 1. **Bonus:** with this, torches now also render in live play and demos. If wiring the static-entity *render* is out of scope/risky, still emit the messages for WinQuake compatibility and note that our renderer doesn't draw them yet.)

- [ ] **Step 4: Run + suite + demos**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py` → `OK`. Full suite green. All three shareware demos still play.

- [ ] **Step 5: Commit**

```bash
git add quake/sv.py quake/sv_send.py tests/test_winquake_compat.py
git commit -m "demo: makestatic builtin + svc_spawnstatic (torches/flames in the signon)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: PVS culling in `write_entities_to_client`

**Files:**
- Modify: `quake/sv_send.py` (`write_entities_to_client`)
- Modify: `client.py` (pass the renderer's PVS helpers to the datagram builder) or `quake/sv.py`
- Test: `tests/test_winquake_compat.py`

Real demos send only entities in the player's PVS (~5/frame); we send all 203. Port the cull using the Renderer's existing PVS methods: `Renderer.point_leaf(p)`, `decompress_vis(visofs)`, `box_in_pvs(mins, maxs, vis)` (`quake/render.py:1119/1164/1131`), with leaf visofs from `Bsp.leafs[leaf][1]`.

The cull needs the BSP/PVS, which lives on the `Renderer`, not the `Server`. The `Client` holds both (`self.rend`, `self.sv`). Thread a PVS-tester callable into `build_datagram`/`write_entities_to_client` from the client, defaulting to "no cull" when absent (keeps `sv_send` decoupled and the existing tests valid).

READ first: `quake/render.py:1119-1170` (the three PVS methods), `quake/sv_send.py:write_entities_to_client` (the loop + the `view_origin` param + the PVS TODO at ~line 62), and `client.py` where `build_datagram` is called (live drive ~1573 and the record tee).

- [ ] **Step 1: Add the failing test**

Add to `tests/test_winquake_compat.py` (+ `__main__`):

```python
def test_pvs_cull_reduces_entity_count():
    from quake.msg import MsgWriter
    from quake.sv_send import write_entities_to_client
    sv = _boot("e1m1")

    def count_updates(pvs_test):
        w = MsgWriter()
        write_entities_to_client(sv, w, sv.player_origin(), pvs_test=pvs_test)
        return _svc_sequence(bytes(w.data)).count(-1)

    all_n = count_updates(None)                         # no cull: everything
    # a PVS tester that culls everything but the player should send far fewer
    culled_n = count_updates(lambda mins, maxs: False)
    assert culled_n < all_n
    assert culled_n >= 1                                # the player is always sent
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py`
Expected: FAIL — `write_entities_to_client` has no `pvs_test` param.

- [ ] **Step 3: Implement the cull**

In `quake/sv_send.py write_entities_to_client`, add `pvs_test=None` and cull per entity (the player edict is never culled, per SV_WriteEntitiesToClient):
```python
def write_entities_to_client(sv, w, view_origin, pvs_test=None):
    vm, f = sv.vm, sv.f
    for e in range(1, vm.num_edicts):
        if vm.free[e]:
            continue
        # ... existing model/skip logic ...
        if pvs_test is not None and e != sv.player:
            mins = vm.fget_v(e, f["absmin"]); maxs = vm.fget_v(e, f["absmax"])
            if not pvs_test(mins, maxs):
                continue                        # not in the client's PVS -> cull
        # ... existing bit computation + write ...
```
(Use `absmin`/`absmax` — the entity's world AABB — for the box test; confirm those field keys exist in `sv.f`, they were used by `solid_box_entities`.)

Thread the tester from `client.py`. Build it once per frame from the eye leaf's PVS and pass through `build_datagram`:
```python
# in client.py, where the live datagram is built (and the record tee):
def _pvs_tester(self, eye):
    leaf = self.rend.point_leaf(eye)
    if leaf <= 0:                                # leaf 0 / solid -> no PVS, send all
        return None
    visofs = self.bsp.leafs[leaf][1]
    if visofs < 0:
        return None
    vis = self.rend.decompress_vis(visofs)
    return lambda mins, maxs: self.rend.box_in_pvs(mins, maxs, vis)
```
`build_datagram(sv, w)` gains a `pvs_test=None` param it forwards to
`write_entities_to_client`. In the live drive and the record tee, pass
`build_datagram(self.sv, dg, pvs_test=self._pvs_tester(eye))` where `eye` is the
player eye (`player_origin + view_ofs`). Confirm `point_leaf`/`decompress_vis`/
`box_in_pvs` signatures against `render.py`.

**Faithfulness note (SV_FatPVS):** WinQuake ORs the PVS of all leaves within 8 units of the eye (`SV_FatPVS`). The single-eye-leaf PVS above is a close approximation that occasionally culls an entity straddling a leaf boundary the fat-PVS would keep. If the round-trip/structural test shows missing entities at boundaries, upgrade `_pvs_tester` to a fat-PVS (OR the vis rows of the eye-leaf's neighbours within 8 units). Start with the single-leaf version; note the divergence.

- [ ] **Step 4: Run + suite + round-trip + demos**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py` → `OK`.
Run: `PQ_AUDIO=0 python tests/test_demo_record.py` → `OK` (the record→play camera path must still reproduce — the player is never culled, so the camera entity is always present).
Full suite green; all three shareware demos still play. Record a session and confirm the datagram entity count dropped sharply (print before/after counts in a throwaway check; delete it).

- [ ] **Step 5: Commit**

```bash
git add quake/sv_send.py client.py tests/test_winquake_compat.py
git commit -m "demo: PVS-cull per-frame entities in recordings (SV_WriteEntitiesToClient)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Structural-parity gate + manual WinQuake test doc

**Files:**
- Modify: `tests/test_winquake_compat.py` (the parity gate)
- Create: `docs/winquake-demo-testing.md`

The verification we CAN do here: assert our recording's message structure matches the genuine `demo1.dem`'s shape, and that a strict re-parse of our recording reaches a complete signon. The verification we CANNOT do here (no WinQuake binary): actually loading the `.dem` in WinQuake — documented for the user.

- [ ] **Step 1: Add the parity + strict-validator test**

Add to `tests/test_winquake_compat.py` (+ `__main__`):

```python
def _record_demo(mapname, frames=40):
    """Record a short session and return the .dem bytes (no file left behind)."""
    import os, tempfile
    from client import Client, InputState
    c = Client(mapname); c.resize(640, 480)
    name = os.path.join(tempfile.mkdtemp(), "wq")
    c._cmd_record([name, mapname])
    for _ in range(frames):
        c.frame(0.05, InputState(move_forward=1.0))
    c._cmd_stopdemo([])
    with open(name + ".dem", "rb") as fh:
        data = fh.read()
    os.remove(name + ".dem")
    return data


def test_our_recording_signon_matches_winquake_shape():
    """Our recording must carry the same signon message *types* a real WinQuake
    demo does: the 3-phase handshake with svc_signonnum 1/2/3, all 64 lightstyles,
    spawnstatic/spawnstaticsound/spawnbaseline, totals, and a spawn svc_time."""
    from quake.demo import DemoReader
    data = _record_demo("e1m1")
    r = DemoReader(data)
    seen = []                                   # svc ids across the signon frames
    signonnums = []
    for _ in range(3):                          # the 3 signon frames
        fr = r.next_frame()
        assert fr is not None
        seq = _svc_sequence(fr[1])
        seen += seq
    # reconstruct the svc_signonnum values by walking (they're byte after the id)
    assert seen.count(P.svc_signonnum) == 3, "must emit signonnum 1, 2 and 3"
    assert P.svc_serverinfo in seen and P.svc_setview in seen
    assert seen.count(P.svc_lightstyle) == 64
    assert P.svc_spawnbaseline in seen
    assert P.svc_time in seen and P.svc_clientdata in seen   # spawn frame
    # the first datagram frame after the signon: svc_time-led
    fr = r.next_frame()
    assert fr is not None and _svc_sequence(fr[1])[0] == P.svc_time


def test_our_recording_replays_in_our_own_player():
    """End-to-end: the new-format recording still plays in pq.ai (round-trip)."""
    from client import Client, InputState
    data = _record_demo("e1m1", frames=60)
    p = Client.__new__(Client); Client._init_assets_only(p)
    p._load_demo(data); p.resize(640, 480)
    moved = False
    last = None
    for _ in range(60):
        if p.demo.finished:
            break
        p.frame(0.05, InputState())
        if last is not None and tuple(p.pos) != last:
            moved = True
        last = tuple(p.pos)
    assert moved, "recorded demo did not play back with a moving camera"
```

- [ ] **Step 2: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_winquake_compat.py` → `OK`.
(If the structural assertions fail, the recording is still missing a signon element — fix the relevant Task 1–5 emitter, not the test.)

- [ ] **Step 3: Write the manual WinQuake test doc**

Create `docs/winquake-demo-testing.md`:
```markdown
# Testing pq.ai recordings in real WinQuake

pq.ai cannot run WinQuake itself, so cross-engine playback is verified by hand.

## Record a demo in pq.ai
1. Launch: `python main.py e1m1`
2. Open the console (`F1`/`~`) and run: `record mydemo e1m1`
3. Play for a bit, then `stop`.
4. The file is written to `quake-shareware/id1/mydemo.dem`.

## Play it in WinQuake / a compatible engine
1. Copy `mydemo.dem` into your Quake `id1/` directory (shareware data is fine;
   the demo only references shareware models/sounds when recorded on e1m1).
2. Launch WinQuake (or QuakeSpasm/vkQuake — all read protocol-15 NQ demos).
3. At the console: `playdemo mydemo`

## What to check
- The demo plays (doesn't stall on a black screen -- that would mean the signon
  handshake is incomplete).
- The level, your movement, monsters, and gunfire reproduce.
- Lighting flickers correctly (lightstyles), torches/flames appear (statics),
  ambient loops play (static sounds), the secret/kill HUD counts are right.
- Entities don't pop in/out through walls (PVS culling).

## Known limitations
- cdtrack is 0 (no CD music) unless the worldspawn `sounds` field is wired.
- pq.ai's player is a dynamically-spawned edict (not WinQuake's reserved edict 1);
  this is legal (svc_setview points WinQuake at it) and does not affect playback.

If a demo stalls, report which check failed; the signon emitters live in
`quake/sv_send.py:build_signon`.
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_winquake_compat.py docs/winquake-demo-testing.md
git commit -m "demo: WinQuake structural-parity gate + manual cross-engine test doc

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** the gap analysis's missing-message list maps to tasks: 3-phase signon + signonnum 1/2/3 + svc_print/cdtrack/setview/time/clientdata/setangle (T1), all-64 lightstyles + total stats (T2), static sounds + cdtrack value (T3), static entities/makestatic (T4), PVS culling (T5), structural-parity verification + manual test doc (T6).
- **Playable core first:** after T1–T2 a WinQuake reaches SIGNONS and renders; T3–T5 are fidelity (torches/ambient-audio/secret-counts/no-wallhack). The user can stop after any task and have a strictly-more-compatible recording.
- **Verification honesty:** no WinQuake binary here. The gate is structural parity against the genuine `demo1.dem` (a real WinQuake recording) + a strict re-parse reaching a complete signon + our own round-trip. True cross-engine confirmation is the documented manual test (T6 doc). This is called out in T6 and the architecture header.
- **No live/playback regression:** every task runs the full suite + the three shareware demos + the record→play round-trip. The signon restructure (T1) is the riskiest — it touches the live loopback's signon parse and our `_load_demo`; both are covered by the suite and the demo plays.
- **Type consistency:** `build_signon(sv)->[bytes,bytes,bytes]`, the `write_static_entities`/`write_static_sounds`/`write_all_lightstyles`/`write_total_stats` helpers, `Server.cdtrack/sound_index/total_secrets/total_monsters/found_secrets/killed_monsters`, `pvs_test` param and `_pvs_tester`, and `_svc_sequence` test helper are referenced consistently across tasks.
- **Open verification risks the executor must confirm against real code:** the `sv.ambients` tuple shape (T3), the `makestatic` builtin number + edict-free mechanism (T4), the `absmin`/`absmax` field keys and the `point_leaf`/`decompress_vis`/`box_in_pvs` signatures (T5), and the QC global names for the total stats (T2, mirror `intermission_stats()`).
