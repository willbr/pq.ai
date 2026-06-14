# Testing pq.ai recordings in real WinQuake

pq.ai cannot run WinQuake itself, so cross-engine playback is verified by hand.
The automated gate (`tests/test_winquake_compat.py`) proves *structural* parity
against the genuine shareware `demo1.dem` (a real WinQuake recording) and a
record->play round-trip in pq.ai's own player; it cannot prove a real WinQuake
client renders the `.dem`. That final confirmation is the manual test below.

## Record a demo in pq.ai

1. Launch: `python main.py e1m2`
   (e1m2 is a good choice -- it has both makestatic torches and ambient loops,
   so the recording exercises `svc_spawnstatic` and `svc_spawnstaticsound`.)
2. Open the console (`F1` on Windows, `~`/backtick on macOS/tkinter) and run:
   `record mydemo e1m2`
3. Play for a bit (walk around, fire, let a monster see you), then `stop`.
4. The file is written to `quake-shareware/id1/mydemo.dem`.

## Play it in WinQuake / a compatible engine

1. Copy `mydemo.dem` into your Quake `id1/` directory. The shareware data is
   fine: a demo recorded on e1m1/e1m2 references only shareware models/sounds.
2. Launch WinQuake (or QuakeSpasm / vkQuake -- all read protocol-15 NetQuake
   demos).
3. At the console: `playdemo mydemo`

## What to check

- **The demo plays** -- it does not stall on a black screen. A black-screen
  stall means the signon handshake never reached `SIGNONS`; that would be a
  missing or out-of-order `svc_signonnum` 1/2/3 (see `quake/sv_send.py`
  `build_signon`).
- The **level geometry** loads and your **movement** reproduces.
- **Monsters and gunfire** reproduce (per-frame entity updates).
- **Lighting flickers** correctly (all 64 lightstyles are sent at spawn).
- **Torches / flames** appear at their fixed spots (`svc_spawnstatic`).
- **Ambient loops** play -- fans, drips, slime hum (`svc_spawnstaticsound`).
- The **secret / kill HUD counts** are right (total stats sent at spawn).
- Entities **don't pop in/out through walls** -- per-frame updates are
  PVS-culled (`SV_WriteEntitiesToClient`).

## Known limitations / expected differences

- **cdtrack** comes from the worldspawn `sounds` field; if your engine has CD
  audio (or a music pack) the correct track plays, otherwise it is silent --
  this does not affect demo playback.
- pq.ai's player is a **dynamically-spawned edict** (not WinQuake's reserved
  edict 1). This is legal: `svc_setview` points the client at the right edict,
  so playback is unaffected.
- pq.ai recordings **omit the scoreboard messages** `svc_updatename`,
  `svc_updatefrags`, and `svc_updatecolors` that a genuine `demo1.dem` carries.
  These set the player's name/frags/colors on the scoreboard; in a fresh
  single-player recording they are cosmetic and their absence does not stall or
  visibly change playback. (The structural test in `tests/test_winquake_compat.py`
  treats these as legitimately optional.)

If a demo stalls or a check fails, report which check failed; the signon
emitters all live in `quake/sv_send.py:build_signon` and the per-frame datagram
in `build_datagram` / `write_entities_to_client`.
