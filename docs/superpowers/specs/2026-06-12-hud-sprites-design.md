# Classic Quake HUD sprites (sbar + ibar) — design

2026-06-12. Approved approach: pure compositor module + WAD parser, blitted by
the client over the indexed framebuffer, with the 3D viewport shrunk by
`sb_lines` rows exactly as WinQuake does.

## Goal

Replace the text status bar in **textured/zbuf mode** with the genuine Quake
status bar drawn from the `gfx.wad` sprites: the 320×24 SBAR strip (armor
icon+number, face with pain/powerup variants, big health number, ammo
icon+number) with the 320×24 IBAR inventory strip above it (weapon row with
selected/pickup-flash states, 8×8 ammo counts, keys, powerup items, sigils).
Wire/flat modes and framebuffers narrower than 320 keep the current text HUD.

## Non-goals

Deathmatch ranking/scoreboard overlays, `+showscores`/SCOREBAR, the mini
deathmatch overlay, CONCHARS console font, menu/help `gfx/*.lmp` pics,
Hipnotic/Rogue variants, drawing the sprite bar in wire/flat modes.

## Module map (mirrors WinQuake)

| New / changed | Ports | Purpose |
|---|---|---|
| `quake/wad.py` (new) | `wad.c` | WAD2 directory + qpic lumps from `gfx.wad` |
| `quake/sbar.py` (new) | `sbar.c` + the bits of `draw.c` it calls | layout + blit into the indexed framebuffer |
| `quake/sv.py` `hud_status()` | `SV_WriteClientdataToMessage` fields | adds raw `items`, raw `weapon` bit, `sigils` (serverflags) |
| `client.py` | `cl_parse.c` timers, `screen.c` sb_lines | faceanim/item-gettime tracking, viewport shrink, wiring |

## quake/wad.py

`Wad(data)` parses the WAD2 header (`"WAD2"`, numlumps, infotableofs) and
32-byte directory entries; lump names case-insensitive (id stores them
uppercase, `sbar.c` asks lowercase). `qpic(name)` returns `(w, h, pixels)` for
type-0x42 lumps: two little-endian int32 (w, h) then `w*h` palette indices.
Palette index 255 is transparent by convention (handled by the blitter, not
the parser). `python -m quake.wad` self-test. No other lump types needed.

## quake/sbar.py

Pure module, no OS/UI imports. `Sbar(wad)` loads the same lump set
`Sbar_Init` does (numbers, anums, colon/slash, weapons ×7 states, ammo,
armor, items, sigils, faces, sbar/ibar). `draw(fb, fbw, fbh, st, time)`
composites into the framebuffer bytearray:

- **Placement** (`Sbar_DrawPic`, single-player branch): bar x-origin is
  `(fbw - 320) >> 1`; sbar occupies rows `fbh-24..fbh`, ibar rows
  `fbh-48..fbh-24`. Any bottom-strip pixels outside the 320 span are filled
  with the 64×64 BACKTILE pattern (`R_DrawTiledPoly` look) so wider
  framebuffers don't show stale view pixels.
- **Blit** (`Draw_Pic`/`Draw_TransPic`): copy palette indices row by row,
  skipping index 255.
- **IBAR** (`Sbar_DrawInventory`): ibar background; weapon slots at x=i*24
  (lightning 48 wide) using `INV_` owned / `INV2_` selected / `INVA1-5_`
  flash for 0.7 s after pickup (`flashon = (time - gettime)*10` clamped,
  exactly the `Sbar_DrawInventory` formula); ammo counts at the top in the
  gold CONCHARS console digits (chars 18+n, as `Sbar_DrawCharacter` does;
  the four pools, 3 digits each at x=(6*i+1)*8-2, blank-padded);
  items (keys, invis/invuln/suit/quad) at x=192+, flashing via gettime;
  sigils at x=320-32+ from the `sigils` bits.
- **SBAR** (`Sbar_DrawNormal`): sbar background; armor: `SB_ARMOR1-3` icon by
  IT_ARMOR bit + 3-digit number at x=24 (red when ≤25; 666 with
  invulnerability, which also swaps the icon for DISC); face at x=112
  (tier `(health-1)//20` clamped, pain variant while `time < faceanimtime`,
  powerup faces for invis/invuln/both/quad); health number at x=136 (red
  ≤25); ammo icon by IT_SHELLS/NAILS/ROCKETS/CELLS at x=224 + count at
  x=248 (red ≤10). Big digits via `Sbar_DrawNum`: 24×24 `NUM_`/`ANUM_`,
  right-aligned 3 digits.
- Health is clamped ≥0 for face tier and shown ≥-99 as in `Sbar_DrawNum`.

## Data plumbing

- `sv.hud_status()` stays backward compatible: existing keys unchanged, new
  keys `items` (raw int bitfield, with the low 4 bits of the `serverflags`
  global folded into bits 28..31 exactly as `SV_WriteClientdataToMessage`
  does) and `weapon_bit` (raw IT_ weapon bit).
- `client.py` keeps the two client-side timers `cl_parse.c` kept:
  - `faceanimtime = sv.time + 0.2` stamped from the same damage event the
    view kick already consumes (`_update_view_feel` path).
  - `item_gettime[bit] = sv.time` stamped by diffing this frame's `items`
    against the previous frame's (`CL_ParseClientdata`), so weapon/item
    pickup flashes work.

## Viewport shrink (sb_lines)

Faithful to `SCR_CalcRefdef`/`R_SetVrect`: when the sprite bar is active,
`sb_lines = 48` and the 3D view renders into `fbw × (fbh - 48)`; the bar owns
the bottom 48 rows. Implementation: `Renderer` gains a `view_height`
(rows of the framebuffer the 3D view uses, default full height) — projection
centre/focal and the per-frame clears/spans use `view_height` while the
framebuffer stays `zw × zh`. The client sets it to `zh - 48` when the bar is
active, full otherwise (mode/resolution changes recompute it). The bar is
opaque over its 320 span and BACKTILE fills the rest, so no stale pixels.

## Activation rules

Sprite bar active iff `mode == "zbuf"` and framebuffer width ≥ 320. When
active, the text status-bar overlay is suppressed (the top diagnostics HUD
line stays). Otherwise behaviour is unchanged. `DEFAULT_VIDEO_RES` becomes
**320×200** and `("320x200", (320, 200))` joins `VIDEO_MODES`; intermission
and death keep drawing the bar exactly as Quake does (no special-casing).

## Performance note

The bar redraws every frame (~320×48 = 15k indices, mostly `bytes` slice
copies per row, not per-pixel Python). The strips and digits only change
when state changes; if profiling shows it matters, cache the composed 320×48
strip keyed on the status tuple and memcpy it in. Start simple; the cache is
a follow-up only if the `present`/`render` profiler buckets show it.

## Testing

Standalone scripts in `tests/`, shareware-booted like the rest:

- `test_wad.py`: gfx.wad parses; known lump count (163); `qpic("sbar")` is
  320×24; `qpic("num_0")` 24×24; case-insensitive lookup; pixel spot-checks.
- `test_sbar.py` (pure, no Client): compose into a blank 320×200 buffer with
  a synthetic status dict; assert SBAR/IBAR background indices landed at the
  right rows, FACE5 pixels at full health vs FACE1 at low, ANUM (red) digits
  when health ≤25, selected-weapon slot uses INV2 pixels, transparent
  (255) pixels leave the background untouched, BACKTILE fills margins on a
  400-wide buffer.
- `test_sbar_client.py`: boot `Client("e1m1")` at 320×200 zbuf — frame's
  framebuffer bottom rows contain the bar; text status overlay absent; at
  240×160 the text overlay is back and no bar; viewport shrink: a frame at
  320×200 with the bar renders the 3D view only into rows 0..151 (e.g.
  centre-of-view sample differs from a no-bar render only below row 152).
- Existing suites must stay green (notably `test_video_menu.py`, which pins
  the 240×160 default today and will be updated to 320×200).
