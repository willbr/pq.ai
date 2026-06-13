# Demo playback & recording via a faithful client/server message loopback

**Date:** 2026-06-13
**Status:** Approved design — ready for implementation planning (Phase 1 first)

## Goal

Add support for playing and recording genuine Quake `.dem` files — including the
shareware `demo1/2/3.dem` inside `pak0.pak` that the title screen loops.

A `.dem` file is a recording of the server→client network message stream. pq.ai
today has **no** such stream: the renderer reads game state directly out of the
in-process server's QuakeC VM edicts (`client.py` calls ~a dozen `self.sv.*`
query methods). There is no `MSG_*` codec, no client-side entity list, no message
boundary at all.

To play genuine demos faithfully — and to record byte-parity demos — we route all
game state through Quake's real protocol (version 15). The server serializes each
frame into an `svc_*` datagram; a loopback hands it to a client parser that builds
a client-side entity list (`cl`); the renderer reads `cl`. **Live play, demo
playback, and recording then become three sources of the same byte stream.**

This is the "full loopback" architecture — the most faithful option, matching
WinQuake's single-player listen-server, where even live play goes
server → build datagram → loopback → `CL_ParseServerMessage` → `cl_entities` →
render.

## Non-goals

- Multiplayer / real sockets. The loopback is in-process only (single client).
- Client-prediction / `clc_move` upload. The local player's intent continues to
  drive the player edict directly as it does today (see "Player input" below);
  we are porting the server→client direction only.
- Cross-engine demo compatibility as a *gate*. Recorded demos target byte-parity
  with WinQuake's output and are verified structurally against the reference C and
  by round-trip; an actual WinQuake byte-diff of identical input is a spot-check,
  not a blocking requirement.

## Reference sources (id GPL, in `quake-source/WinQuake/`)

- `protocol.h` — `svc_*` (0–34), `U_*` entity-update bits, `SU_*` clientdata bits,
  `PROTOCOL_VERSION 15`, `DEFAULT_VIEWHEIGHT 22`.
- `common.c:510-725` — `MSG_Write*` / `MSG_Read*`. Coord = `short(f*8)`;
  angle = `byte(f*256/360)`. Little-endian throughout.
- `sv_main.c` — `SV_CreateBaseline` (925), `SV_WriteEntitiesToClient` (427),
  `SV_WriteClientdataToMessage` (576), `SV_UpdateToReliableMessages` (756),
  `SV_SendClientDatagram` (720), `SV_SendClientMessages` (819).
- `host.c:633+` — host-loop ordering: `SV_Physics` runs before
  `SV_SendClientMessages`; the client reads after.
- `net_loop.c` — loopback message framing (single-player has no sockets).
- `cl_parse.c` — `CL_ParseServerMessage` (720), `CL_ParseServerInfo` (204),
  `CL_ParseUpdate` (330), `CL_ParseClientdata` (514), `CL_ParseBaseline` (491),
  `CL_ParseStatic`, `CL_RelinkEntities` (442).
- `client.h:148-235` — `client_state_t` (`cl_entities`, `mtime[2]`, `time`,
  `viewangles`, `stats`, `items`, `model_precache`, `sound_precache`, statics).
- `cl_demo.c` — `.dem` framing, `CL_PlayDemo_f`, `CL_GetMessage` (demo timing
  gate), `CL_WriteDemoMessage`, `CL_NextDemo`/`startdemos`, timedemo report.

When porting, cite origin in docstrings/commits as the standing convention.

## Architecture

### New modules (pure; no OS/UI; live in `quake/`, relative imports)

1. **`quake/msg.py`** — `MsgWriter` and `MsgReader`.
   - Writer: `byte, char, short, long, float, string(s), coord(f), angle(f)`.
   - Reader: the inverses, plus `at_end`.
   - Encodings exactly per `common.c`: `coord` → `short(int(f*8))`,
     `angle` → `byte(int(f)*256//360 & 255)`; little-endian; `string` is
     NUL-terminated. Read coord = `short/8.0`, read angle = `char*(360/256)`.
   - Pure and standalone — the first thing built and tested.

2. **`quake/protocol.py`** — numeric constants only:
   `svc_*`, `U_*`, `SU_*`, `STAT_*`, `PROTOCOL_VERSION=15`,
   `DEFAULT_VIEWHEIGHT=22`, temp-entity subtypes (`TE_*`).

3. **Server send side** — `quake/sv_send.py` (free functions taking a `Server`),
   or new methods on `Server`. Decision deferred to the plan; logic is identical.
   - `create_baseline()` — snapshot each spawned edict's
     `{modelindex, frame, colormap, skin, origin, angles}`; emit
     `svc_spawnbaseline` into the signon buffer. Stored on the server so deltas
     can diff against it (no baseline store exists today — this is new state).
   - `write_entities_to_client(writer, view_origin)` — PVS-cull against the
     player's view leaf, then per edict compute `U_*` bits by diffing live state
     vs baseline, and write the delta (`sv_main.c:427-549` order exactly).
   - `write_clientdata_to_message(writer)` — `svc_clientdata`: `SU_*` bits for
     viewheight/idealpitch/punch/velocity/weaponframe/armor/weapon, then the
     always-sent items(long)/health(short)/ammo/weapon. Sources already exist in
     `hud_status()` / player edict fields.
   - `reliable_messages(writer)` — `svc_updatestat` diffs, lightstyle changes,
     centerprint, stufftext("bf"), `svc_intermission`/`svc_finale`,
     `svc_cdtrack`, `svc_setangle` on teleport.
   - `build_datagram(writer)` — the per-frame unreliable message:
     `svc_time`+float, then clientdata, then entities, then appended unreliable
     events accumulated during the frame (sounds, temp entities, `svc_particle`
     bursts). Mirrors `SV_SendClientDatagram`.
   - `write_serverinfo(writer)` — signon: `svc_serverinfo` (protocol, maxclients,
     gametype, levelname, model precache list, sound precache list),
     then baselines and `svc_spawnstatic`, then `svc_signonnum`.

   The server already holds all the source state: edict fields via
   `vm.fget_f/v/i` + the `f[]` offset dict; `model_precache`/`sound_precache`;
   `lightstyles`; `center_msg`; `bonus_flash`; `intermission_active()` /
   `intermission_stats()`; `changelevel`. The work is **serialization**, not new
   game logic.

4. **`quake/cl_parse.py` + `ClientState`** — the client half.
   - `ClientState` (`cl`) holds: `entities[]` (each with baseline, current +
     previous `msg_origins`/`msg_angles`, `msgtime`, model/frame/skin/colormap/
     effects), `static_entities[]`, `stats[]`, `items`, `lightstyles{}`,
     `model_precache[]`, `sound_precache[]`, `viewangles`, `viewentity`,
     `view_height`, `punchangle`, `velocity`, `mtime[2]`, `time`,
     `intermission`, `levelname`, `cdtrack`.
   - `parse_message(reader)` — `CL_ParseServerMessage` dispatch: the high-bit
     fast path → `parse_update`; the `svc_*` switch for everything else.
   - `parse_serverinfo` / `parse_update` (delta vs baseline) /
     `parse_clientdata` / `parse_baseline` / `parse_static` / `parse_sound` /
     `parse_particle` / `parse_temp_entity` / lightstyle / setangle / setview /
     centerprint / intermission / time.
   - `relink(frac)` — `CL_RelinkEntities`: lerp each entity between its last two
     messages (teleport guard: delta > 100 units → snap); lerp player velocity;
     **generate client-side trails** from entity model-flags/effects (rocket,
     grenade, gib, etc.) — see "Particles" below.
   - The renderer reads off `cl`: a thin adapter exposes the same shapes the
     renderer already consumes (`alias_entities`, `sprite_entities`,
     `bsp_model_entities`, `brush_models`, `light_entities`, particles,
     lightstyles, hud/stats), now sourced from `cl_entities` instead of `sv`.

5. **`quake/demo.py`** — `.dem` framing.
   - Read: parse the CD-track header line (chars to `\n`), then repeated frames
     `[u32 len][3×f32 viewangles][len bytes message]`.
   - Write: the same framing, prefixed by the CD-track header.
   - A `DemoReader` yields `(viewangles, message_bytes)`; a `DemoWriter` appends.

### Loopback

Single-player needs no real net framing — the datagram bytes can be handed
straight from `build_datagram`'s writer to `cl.parse_message`. We keep a trivial
in-process queue object so the same call site works for live, demo (bytes from
file), and (future) any other source. `net_loop.c`'s 4-byte alignment framing is
**not** required for the in-process path and will be omitted unless a `.dem`
round-trip shows we need it (we do not — demo framing is its own format).

### Host-loop reorder (`client.py`)

`Client.frame(dt, inp)` today: apply input → run QC server → read sv edicts →
build `RenderFrame`. It becomes (Phase 1):

1. Apply input to the player edict (unchanged — player intent still drives the
   edict directly; we are not porting `clc_move`).
2. `sv.run_frame(dt)` — QC tick + physics, exactly as now.
3. `writer = MsgWriter(); sv.build_datagram(writer)` (plus signon on first frame
   / after changelevel).
4. `cl.parse_message(MsgReader(writer.bytes))`.
5. `frac = cl.lerp_point(); cl.relink(frac)`.
6. Build `RenderFrame` from `cl` (view origin/angles, entities, stats,
   particles, lightstyles) instead of from `sv`.

`SV_Physics` before send, client read after — matching `host.c`.

## Key consequences (accepted)

1. **Entity interpolation.** Rendering lerps entities between the last two
   messages, so live play gains Quake's one-message smoothing instead of today's
   exact-position reads. More faithful; also what makes high-FPS demo playback
   smooth. Slightly changes live feel (one message of latency).

2. **Particles/trails move client-side.** Quake sends only `svc_particle`
   bursts; rocket/blood *trails* are generated on the client during relink from
   entity model-flags/effects. pq.ai currently owns the entire particle system
   server-side (`_emit_trails`, `_advance_particles`, `self.particles`).
   Byte-parity forces the Quake split:
   - The server emits `svc_particle` only for explicit `particle()` builtin calls.
   - Trails become a **client-side** system on `cl`, keyed off entity effects,
     advanced each frame by the client.
   - The integration/aging of particles moves from `sv` to `cl`.
   Dynamic lights are already client-side here (`client.py` `_update_dlights`
   reads `light_entities()` / `dlight_events`) — they stay, re-sourced from `cl`.

3. **Baselines are new server state.** `create_baseline` snapshots spawn-time
   edict state; without it, deltas and `svc_spawnbaseline` cannot be encoded.

## Phasing

Each phase is independently shippable and verifiable.

### Phase 1 — Loopback (the foundation; bulk of the work and regression risk)
Live single-player routes through msg → datagram → parse → `cl`; the renderer
reads `cl`. No demos yet. The game plays identically modulo interpolation.
Includes: `msg.py`, `protocol.py`, the server send side, `cl_parse.py` +
`ClientState`, the client-side particle/trail system, and the `client.py`
reorder removing the direct `sv.*` render reads.

**Done when:** the game boots and plays on `e1m1` (movement, doors, monsters,
combat, pickups, HUD, intermission, changelevel) reading from `cl`; all existing
`tests/test_*.py` pass; new codec + datagram round-trip tests pass.

### Phase 2 — Playback
Feed `cl.parse_message` from a `DemoReader` instead of the live datagram; gate
message reads on `cl.time > cl.mtime[0]` (`cl_demo.c`). Add `playdemo <name>`
(from pak or file) and `stop` console commands, `timedemo <name>` (read one
message per frame, report avg FPS via the existing `PROFILER`), and the
title-screen demo loop (`startdemos demo1 demo2 demo3` when launched as
`python main.py start` with no map). Playback switches the host loop to skip the
QC tick/physics and drive `cl` from the file.

**Done when:** `demo1/2/3.dem` from the shareware `pak0.pak` play correctly;
`timedemo demo1` reports an FPS figure; the title loop cycles the three demos.

### Phase 3 — Recording
Tee the datagram (and the signon) the loopback already builds into a
`DemoWriter`. Add `record <name> <map>` (start the map, begin recording) and
`stop` (finish). Match WinQuake message ordering and precache lists.

**Done when:** `record … → stop → playdemo …` round-trips in pq.ai; the output
framing and message order match the reference C on inspection.

## Error handling

- **Codec:** `MsgReader` past end-of-buffer raises a clear `EOFError`-style
  exception; `parse_message` treats a truncated message as end-of-message
  (Quake returns on `cmd == -1`).
- **Demo files:** a missing/short/corrupt `.dem`, a bad header line, or an
  unknown protocol version prints a console error and aborts playback cleanly
  (back to the console/title), never crashes the frame loop.
- **Unknown `svc_*`:** raise with the offending byte and message offset — during
  development this surfaces protocol bugs immediately rather than desyncing
  silently.
- **Missing map for `record`:** reuse `_load_map`'s existing "not in this pak"
  path; refuse to start recording.

## Testing strategy

- `tests/test_msg.py` — encode→decode round-trip for every primitive; known
  byte vectors lifted from `common.c` (coord `*8`, angle `*256/360`, LE order).
- `tests/test_datagram_roundtrip.py` — boot the real stack (`_boot()` pattern),
  run a frame, `build_datagram` → `parse_message`, assert `cl_entities` state
  matches the server edicts (origin within coord quantization, frame, model,
  effects).
- `tests/test_cl_parse.py` — feed hand-built messages, assert `cl` state.
- `tests/test_demo_framing.py` — `DemoWriter`→`DemoReader` round-trip; parse the
  real `demo1.dem` header and first frame from `pak0.pak`.
- `tests/test_demo_playback.py` — smoke-play `demo1.dem` for N frames without
  exception; spot-check a known entity position.
- `tests/test_record_roundtrip.py` — record a scripted session, play it back,
  compare entity trajectories.
- All existing tests stay green (the Phase 1 reorder must not regress them).
- Run muted: `PQ_AUDIO=0`.

## Open implementation details (resolved during planning, not blocking)

- Whether the server send side is a module (`sv_send.py`) or methods on `Server`.
- Exact representation of `cl_entities` (dataclass vs parallel arrays) — chosen
  for renderer-adapter convenience and perf.
- Whether interpolation needs `mtime` smoothing tuning for the demo cadence vs
  the live one-message-per-frame cadence.
